"""API error classification + assistant-error-message construction.

Four main functions:

- :func:`is_prompt_too_long_message` — message-level predicate.
- :func:`get_assistant_message_from_error` — maps a raw exception to an **AssistantMessage**
  envelope (``isApiErrorMessage=True``) via
  :func:`tabvis.utils.messages.create_assistant_api_error_message`. This is the *assistant* error
  constructor (surfaces to ``-p`` as ``assistant.error``), NOT the ``system`` retry sentinel.
- :func:`classify_api_error` — returns a short ``str`` tag for analytics.
- :func:`get_error_message_if_refusal` — returns an AssistantMessage **only** when
  ``stop_reason == 'refusal'``, else ``None``.

SDK error-class notes:
- ``APIConnectionTimeoutError`` is a subclass of :class:`anthropic.APITimeoutError` /
  ``APIConnectionError``.
- ``error.status_code`` is present only on ``APIStatusError`` instances — read via
  :func:`_status_code`.
- Response headers are read via ``error.response.headers.get(...)`` — see :func:`_header`.
- The error message is read via ``getattr(err, "message", None) or str(err)`` — see
  :func:`_error_message`.

Deeper transitive dependencies are stubbed with sensible defaults (rate-limit message generation,
API provider, 3P fallback suggestions, API-key-source resolution, connection-error detail
formatting, bootstrap non-interactive state).
"""

from __future__ import annotations

import os
import re
from typing import Any

import anthropic
from anthropic import APIConnectionError, APIError, APITimeoutError

from tabvis.bootstrap.state import (
    get_is_non_interactive_session as _bootstrap_get_is_non_interactive_session,
)
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.messages import create_assistant_api_error_message
from tabvis.utils.model.model import get_model_strings

# --------------------------------------------------------------------------------------------
# Constants (user-facing error strings + prefixes)
# --------------------------------------------------------------------------------------------

API_ERROR_MESSAGE_PREFIX = "API Error"
PROMPT_TOO_LONG_ERROR_MESSAGE = "Prompt is too long"
CREDIT_BALANCE_TOO_LOW_ERROR_MESSAGE = "Credit balance is too low"
INVALID_API_KEY_ERROR_MESSAGE = "Not authenticated · Set TABVIS_API_KEY"
INVALID_API_KEY_ERROR_MESSAGE_EXTERNAL = "Invalid API key · Fix external API key"
ORG_DISABLED_ERROR_MESSAGE_ENV_KEY = (
    "Your TABVIS_API_KEY belongs to a disabled organization · "
    "Update or unset the environment variable"
)
CCR_AUTH_ERROR_MESSAGE = (
    "Authentication error · This may be a temporary network issue, please try again"
)
REPEATED_529_ERROR_MESSAGE = "Repeated 529 Overloaded errors"
CUSTOM_OFF_SWITCH_MESSAGE = (
    "TABVIS Max is experiencing high load, please use /model to switch to TABVIS Balanced"
)
API_TIMEOUT_ERROR_MESSAGE = "Request timed out"

# Interactive-only recovery hint appended to several 400 tool-use error messages.
_REWIND_INSTRUCTION = " Run /rewind to recover the conversation."

# Sentinel returned when a rate-limit fallback handles the situation silently (Opus -> Sonnet).
NO_RESPONSE_REQUESTED = "No response requested."

# ``AFK_MODE_BETA_HEADER`` is False (inert in this build).
AFK_MODE_BETA_HEADER: str | bool = False

__all__ = [
    "API_ERROR_MESSAGE_PREFIX",
    "API_TIMEOUT_ERROR_MESSAGE",
    "CCR_AUTH_ERROR_MESSAGE",
    "CREDIT_BALANCE_TOO_LOW_ERROR_MESSAGE",
    "CUSTOM_OFF_SWITCH_MESSAGE",
    "INVALID_API_KEY_ERROR_MESSAGE",
    "INVALID_API_KEY_ERROR_MESSAGE_EXTERNAL",
    "ORG_DISABLED_ERROR_MESSAGE_ENV_KEY",
    "PROMPT_TOO_LONG_ERROR_MESSAGE",
    "REPEATED_529_ERROR_MESSAGE",
    "classify_api_error",
    "get_assistant_message_from_error",
    "get_error_message_if_refusal",
    "is_prompt_too_long_message",
    "starts_with_api_error_prefix",
]


# --------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------


def _get_is_non_interactive_session() -> bool:
    """Delegates to the bootstrap-state singleton; headless ``-p`` runs are non-interactive."""
    return _bootstrap_get_is_non_interactive_session()


def _is_ccr_mode() -> bool:
    """Tabvis Remote mode auths via JWTs, not /login."""
    return is_env_truthy(os.environ.get("TABVIS_REMOTE"))


def _get_model_api_key_source() -> str | None:
    """Resolve the source of the model API key.

    The full auth chain (keychain/config/apiKeyHelper) is not implemented in this build; only the
    ``TABVIS_API_KEY`` env var is resolved, and any other source returns ``None`` here.
    """
    if os.environ.get("TABVIS_API_KEY"):
        return "TABVIS_API_KEY"
    return None


def _get_api_provider() -> str:
    """First-party API provider only.

    Bedrock/Vertex/Foundry provider detection is not supported in this build.
    """
    return "firstParty"


def _get_rate_limit_error_message() -> str | None:
    """Quota-aware 429 copy.

    Quota-aware rate-limit messaging is not implemented in this build; this returns a generic
    rate-limit string for the new-header path (never the silent ``None`` fallback).
    """
    return "Rate limit reached"


def _format_api_error(error: APIError) -> str:
    """Connection-error detail formatting.

    SSL/TLS/code branch formatting is not implemented in this build; this falls back to the raw
    error message.
    """
    return _error_message(error)


def _is_ssl_connection_error(error: APIConnectionError) -> bool:
    """Best-effort detection of an SSL/certificate connection error.

    Cause-chain code extraction is not implemented in this build; this sniffs the message text.
    """
    msg = _error_message(error).lower()
    return "ssl" in msg or "certificate" in msg or "cert" in msg


def _get_3p_model_fallback_suggestion(model: str) -> str | None:
    """Only fires for non-first-party providers."""
    if _get_api_provider() == "firstParty":
        return None
    strings = get_model_strings()
    m = model.lower()
    if "opus-4-6" in m or "opus_4_6" in m:
        return strings.get("opus41")
    if "sonnet-4-6" in m or "sonnet_4_6" in m:
        return strings.get("sonnet45")
    if "sonnet-4-5" in m or "sonnet_4_5" in m:
        return strings.get("sonnet40")
    return None


# --------------------------------------------------------------------------------------------
# Error-shape accessors (status code / message / headers)
# --------------------------------------------------------------------------------------------


def _status_code(error: Any) -> int | None:
    """Read ``error.status_code`` (present on ``APIStatusError`` instances)."""
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _error_message(error: Any) -> str:
    """SDK errors expose ``.message``; fall back to ``str(error)`` otherwise."""
    msg = getattr(error, "message", None)
    if isinstance(msg, str):
        return msg
    return str(error)


def _header(error: Any, name: str) -> str | None:
    """Read a response header (``error.response.headers.get(name)``), guarded against absence."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


def _is_connection_timeout(error: Any) -> bool:
    """True for an ``APITimeoutError``, or an ``APIConnectionError`` with 'timeout' in the message."""
    if isinstance(error, APITimeoutError):
        return True
    return isinstance(error, APIConnectionError) and "timeout" in _error_message(error).lower()


# --------------------------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------------------------


def starts_with_api_error_prefix(text: str) -> bool:
    """True if ``text`` starts with the canonical API-error prefix."""
    return text.startswith(API_ERROR_MESSAGE_PREFIX)


def is_prompt_too_long_message(msg: dict[str, Any]) -> bool:
    """``True`` iff ``msg`` is an api-error message whose content has a text block starting with the
    canonical prompt-too-long prefix. ``msg`` is an AssistantMessage envelope (plain dict).
    """
    if not msg.get("isApiErrorMessage"):
        return False
    content = msg.get("message", {}).get("content")
    if not isinstance(content, list):
        return False
    return any(
        block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].startswith(PROMPT_TOO_LONG_ERROR_MESSAGE)
        for block in content
    )


def _pdf_too_large_message() -> str:
    # Note: this message doesn't embed the exact page limit or a formatted file size, since those
    # values aren't tracked here.
    if _get_is_non_interactive_session():
        return (
            "PDF too large. Try reading the file a different way "
            "(e.g., extract text with pdftotext)."
        )
    return (
        "PDF too large. Double press esc to go back and try again, or use pdftotext to "
        "convert to text first."
    )


def _pdf_password_protected_message() -> str:
    if _get_is_non_interactive_session():
        return "PDF is password protected. Try using a CLI tool to extract or convert the PDF."
    return (
        "PDF is password protected. Please double press esc to edit your message and try again."
    )


def _pdf_invalid_message() -> str:
    if _get_is_non_interactive_session():
        return "The PDF file was not valid. Try converting it to text first (e.g., pdftotext)."
    return (
        "The PDF file was not valid. Double press esc to go back and try again with a "
        "different file."
    )


def _image_too_large_message() -> str:
    if _get_is_non_interactive_session():
        return "Image was too large. Try resizing the image or using a different approach."
    return (
        "Image was too large. Double press esc to go back and try again with a smaller image."
    )


def _request_too_large_message() -> str:
    if _get_is_non_interactive_session():
        return "Request too large. Try with a smaller file."
    return (
        "Request too large. Double press esc to go back and try with a smaller file."
    )


def get_assistant_message_from_error(
    error: Any,
    model: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Maps a raw exception to an **AssistantMessage** envelope (``isApiErrorMessage=True``) built by
    :func:`create_assistant_api_error_message`. Branches are checked in order and the first
    matching condition wins.

    Note: ``ImageSizeError``/``ImageResizeError`` (pre-API validation) and the tool_use/tool_result
    mismatch analytics-logging context (``options``) are no-ops in this build.
    """
    # SDK timeout errors.
    if _is_connection_timeout(error):
        return create_assistant_api_error_message(
            content=API_TIMEOUT_ERROR_MESSAGE,
            error="unknown",
        )

    # Emergency capacity off switch for Opus PAYG users.
    if isinstance(error, Exception) and CUSTOM_OFF_SWITCH_MESSAGE in _error_message(error):
        return create_assistant_api_error_message(
            content=CUSTOM_OFF_SWITCH_MESSAGE,
            error="rate_limit",
        )

    if isinstance(error, APIError) and _status_code(error) == 429:
        rate_limit_type = _header(error, "anthropic-ratelimit-unified-representative-claim")
        if rate_limit_type:
            specific = _get_rate_limit_error_message()
            if specific:
                return create_assistant_api_error_message(
                    content=specific,
                    error="rate_limit",
                )
            # Silent fallback (e.g. Opus -> Sonnet): record but show nothing.
            return create_assistant_api_error_message(
                content=NO_RESPONSE_REQUESTED,
                error="rate_limit",
            )

        # No quota headers — surface what the API said.
        message = _error_message(error)
        if "Extra usage is required for long context" in message:
            hint = "use --model to switch to standard context"
            return create_assistant_api_error_message(
                content=(
                    f"{API_ERROR_MESSAGE_PREFIX}: Extra usage is required for 1M context · {hint}"
                ),
                error="rate_limit",
            )
        stripped = re.sub(r"^429\s+", "", message)
        inner = re.search(r'"message"\s*:\s*"([^"]*)"', stripped)
        detail = inner.group(1) if inner else stripped
        tail = detail or "this may be a temporary capacity issue — check provider status page"
        return create_assistant_api_error_message(
            content=f"{API_ERROR_MESSAGE_PREFIX}: Request rejected (429) · {tail}",
            error="rate_limit",
        )

    # Prompt too long.
    if isinstance(error, Exception) and "prompt is too long" in _error_message(error).lower():
        return create_assistant_api_error_message(
            content=PROMPT_TOO_LONG_ERROR_MESSAGE,
            error="invalid_request",
            error_details=_error_message(error),
        )

    # PDF page-limit errors.
    if isinstance(error, Exception) and re.search(
        r"maximum of \d+ PDF pages", _error_message(error)
    ):
        return create_assistant_api_error_message(
            content=_pdf_too_large_message(),
            error="invalid_request",
            error_details=_error_message(error),
        )

    # Password-protected PDF.
    if isinstance(error, Exception) and "The PDF specified is password protected" in _error_message(
        error
    ):
        return create_assistant_api_error_message(
            content=_pdf_password_protected_message(),
            error="invalid_request",
        )

    # Invalid PDF.
    if isinstance(error, Exception) and "The PDF specified was not valid" in _error_message(error):
        return create_assistant_api_error_message(
            content=_pdf_invalid_message(),
            error="invalid_request",
        )

    # Image size error (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "image exceeds" in _error_message(error)
        and "maximum" in _error_message(error)
    ):
        return create_assistant_api_error_message(
            content=_image_too_large_message(),
            error_details=_error_message(error),
        )

    # Many-image dimension error (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "image dimensions exceed" in _error_message(error)
        and "many-image" in _error_message(error)
    ):
        return create_assistant_api_error_message(
            content=(
                "An image in the conversation exceeds the dimension limit for many-image "
                "requests (2000px). Start a new session with fewer images."
                if _get_is_non_interactive_session()
                else "An image in the conversation exceeds the dimension limit for many-image "
                "requests (2000px). Run /compact to remove old images from context, or start a "
                "new session."
            ),
            error="invalid_request",
            error_details=_error_message(error),
        )

    # Server rejected the afk-mode beta header (inert while AFK_MODE_BETA_HEADER is falsy).
    if (
        AFK_MODE_BETA_HEADER
        and isinstance(error, APIError)
        and _status_code(error) == 400
        and isinstance(AFK_MODE_BETA_HEADER, str)
        and AFK_MODE_BETA_HEADER in _error_message(error)
        and "anthropic-beta" in _error_message(error)
    ):
        return create_assistant_api_error_message(
            content="Auto mode is unavailable for your plan",
            error="invalid_request",
        )

    # Request too large (413).
    if isinstance(error, APIError) and _status_code(error) == 413:
        return create_assistant_api_error_message(
            content=_request_too_large_message(),
            error="invalid_request",
        )

    # tool_use/tool_result concurrency error (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "`tool_use` ids were found without `tool_result` blocks immediately after"
        in _error_message(error)
    ):
        base_message = "API Error: 400 due to tool use concurrency issues."
        rewind = "" if _get_is_non_interactive_session() else _REWIND_INSTRUCTION
        return create_assistant_api_error_message(
            content=base_message + rewind,
            error="invalid_request",
        )

    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "unexpected `tool_use_id` found in `tool_result`" in _error_message(error)
    ):
        pass

    # Duplicate tool_use IDs (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "`tool_use` ids must be unique" in _error_message(error)
    ):
        rewind = "" if _get_is_non_interactive_session() else _REWIND_INSTRUCTION
        return create_assistant_api_error_message(
            content=f"API Error: 400 duplicate tool_use ID in conversation history.{rewind}",
            error="invalid_request",
            error_details=_error_message(error),
        )

    # Credit balance too low.
    if isinstance(error, Exception) and "Your credit balance is too low" in _error_message(error):
        return create_assistant_api_error_message(
            content=CREDIT_BALANCE_TOO_LOW_ERROR_MESSAGE,
            error="billing_error",
        )

    # Organization disabled (400) — commonly a stale TABVIS_API_KEY.
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "organization has been disabled" in _error_message(error).lower()
    ):
        if _get_model_api_key_source() == "TABVIS_API_KEY" and os.environ.get("TABVIS_API_KEY"):
            return create_assistant_api_error_message(
                error="invalid_request",
                content=ORG_DISABLED_ERROR_MESSAGE_ENV_KEY,
            )

    # x-api-key authentication errors.
    if isinstance(error, Exception) and "x-api-key" in _error_message(error).lower():
        if _is_ccr_mode():
            return create_assistant_api_error_message(
                error="authentication_failed",
                content=CCR_AUTH_ERROR_MESSAGE,
            )
        source = _get_model_api_key_source()
        is_external = source in ("TABVIS_API_KEY", "apiKeyHelper")
        return create_assistant_api_error_message(
            error="authentication_failed",
            content=(
                INVALID_API_KEY_ERROR_MESSAGE_EXTERNAL
                if is_external
                else INVALID_API_KEY_ERROR_MESSAGE
            ),
        )

    # Generic 401/403 authentication errors.
    if isinstance(error, APIError) and _status_code(error) in (401, 403):
        if _is_ccr_mode():
            return create_assistant_api_error_message(
                error="authentication_failed",
                content=CCR_AUTH_ERROR_MESSAGE,
            )
        return create_assistant_api_error_message(
            error="authentication_failed",
            content=(
                f"Failed to authenticate. {API_ERROR_MESSAGE_PREFIX}: {_error_message(error)}"
                if _get_is_non_interactive_session()
                else f"Set TABVIS_API_KEY · {API_ERROR_MESSAGE_PREFIX}: {_error_message(error)}"
            ),
        )

    # 404 Not Found — model unavailable.
    if isinstance(error, APIError) and _status_code(error) == 404:
        switch_cmd = "--model" if _get_is_non_interactive_session() else "/model"
        fallback = _get_3p_model_fallback_suggestion(model)
        return create_assistant_api_error_message(
            content=(
                f"The model {model} is not available on your {_get_api_provider()} deployment. "
                f"Try {switch_cmd} to switch to {fallback}, or ask your admin to enable this model."
                if fallback
                else f"There's an issue with the selected model ({model}). It may not exist or "
                f"you may not have access to it. Run {switch_cmd} to pick a different model."
            ),
            error="invalid_request",
        )

    # Connection errors (non-timeout).
    if isinstance(error, APIConnectionError):
        return create_assistant_api_error_message(
            content=f"{API_ERROR_MESSAGE_PREFIX}: {_format_api_error(error)}",
            error="unknown",
        )

    if isinstance(error, Exception):
        return create_assistant_api_error_message(
            content=f"{API_ERROR_MESSAGE_PREFIX}: {_error_message(error)}",
            error="unknown",
        )

    return create_assistant_api_error_message(
        content=API_ERROR_MESSAGE_PREFIX,
        error="unknown",
    )


def classify_api_error(error: Any) -> str:
    """Short error-type tag for analytics."""
    # Aborted requests.
    if isinstance(error, Exception) and _error_message(error) == "Request was aborted.":
        return "aborted"

    # Timeout errors.
    if _is_connection_timeout(error):
        return "api_timeout"

    # Repeated 529 errors.
    if isinstance(error, Exception) and REPEATED_529_ERROR_MESSAGE in _error_message(error):
        return "repeated_529"

    # Emergency capacity off switch.
    if isinstance(error, Exception) and CUSTOM_OFF_SWITCH_MESSAGE in _error_message(error):
        return "capacity_off_switch"

    # Rate limiting.
    if isinstance(error, APIError) and _status_code(error) == 429:
        return "rate_limit"

    # Server overload (529).
    if isinstance(error, APIError) and (
        _status_code(error) == 529 or '"type":"overloaded_error"' in _error_message(error)
    ):
        return "server_overload"

    # Prompt/content size errors.
    if (
        isinstance(error, Exception)
        and PROMPT_TOO_LONG_ERROR_MESSAGE.lower() in _error_message(error).lower()
    ):
        return "prompt_too_long"

    # PDF errors.
    if isinstance(error, Exception) and re.search(r"maximum of \d+ PDF pages", _error_message(error)):
        return "pdf_too_large"

    if isinstance(error, Exception) and "The PDF specified is password protected" in _error_message(
        error
    ):
        return "pdf_password_protected"

    # Image size errors (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "image exceeds" in _error_message(error)
        and "maximum" in _error_message(error)
    ):
        return "image_too_large"

    # Many-image dimension errors (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "image dimensions exceed" in _error_message(error)
        and "many-image" in _error_message(error)
    ):
        return "image_too_large"

    # Tool use errors (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "`tool_use` ids were found without `tool_result` blocks immediately after"
        in _error_message(error)
    ):
        return "tool_use_mismatch"

    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "unexpected `tool_use_id` found in `tool_result`" in _error_message(error)
    ):
        return "unexpected_tool_result"

    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "`tool_use` ids must be unique" in _error_message(error)
    ):
        return "duplicate_tool_use_id"

    # Invalid model errors (400).
    if (
        isinstance(error, APIError)
        and _status_code(error) == 400
        and "invalid model name" in _error_message(error).lower()
    ):
        return "invalid_model"

    # Credit/billing errors.
    if (
        isinstance(error, Exception)
        and CREDIT_BALANCE_TOO_LOW_ERROR_MESSAGE.lower() in _error_message(error).lower()
    ):
        return "credit_balance_low"

    # x-api-key authentication errors.
    if isinstance(error, Exception) and "x-api-key" in _error_message(error).lower():
        return "invalid_api_key"

    # Generic auth errors.
    if isinstance(error, APIError) and _status_code(error) in (401, 403):
        return "auth_error"

    # Status-code based fallbacks.
    if isinstance(error, APIError):
        status = _status_code(error)
        if status is not None and status >= 500:
            return "server_error"
        if status is not None and status >= 400:
            return "client_error"

    # Connection errors — SSL/TLS first.
    if isinstance(error, APIConnectionError):
        if _is_ssl_connection_error(error):
            return "ssl_cert_error"
        return "connection_error"

    return "unknown"


def categorize_retryable_api_error(error: APIError) -> str:
    """Short error-category tag used for retry-sentinel messages."""
    status = _status_code(error)
    if status == 529 or '"type":"overloaded_error"' in _error_message(error):
        return "rate_limit"
    if status == 429:
        return "rate_limit"
    if status in (401, 403):
        return "authentication_failed"
    if status is not None and status >= 408:
        return "server_error"
    return "unknown"


def get_error_message_if_refusal(
    stop_reason: str | None,
    model: str,
) -> dict[str, Any] | None:
    """Returns an AssistantMessage envelope **only** when ``stop_reason == 'refusal'``; otherwise
    ``None``. (model_client yields this at ``message_delta`` when the API refuses.)
    """
    if stop_reason != "refusal":
        return None

    base_message = (
        f"{API_ERROR_MESSAGE_PREFIX}: Tabvis is unable to respond to this request, which appears "
        "to violate our Usage Policy (the configured provider policy). Try rephrasing the request "
        "or attempting a different approach."
        if _get_is_non_interactive_session()
        else f"{API_ERROR_MESSAGE_PREFIX}: Tabvis is unable to respond to this request, which "
        "appears to violate our Usage Policy (the configured provider policy). Please double "
        "press esc to edit your last message or start a new session for Tabvis to assist with a "
        "different task."
    )

    model_suggestion = (
        " If you are seeing this refusal repeatedly, try running /model tabvis-balanced to "
        "switch models."
        if model != "claude-sonnet-4-20250514"
        else ""
    )

    return create_assistant_api_error_message(
        content=base_message + model_suggestion,
        error="invalid_request",
    )


# Re-export for use in type annotations / call sites elsewhere in this module.
APIStatusError = anthropic.APIStatusError
