"""Retry / backoff wrapper for model API calls.

Protocol: since Python async generators cannot ``return`` a value to ``async for``, results are
reified as a stream of **tagged dataclasses**:

- :class:`RetryError` wraps a ``system`` retry-heartbeat sentinel (the
  :func:`tabvis.utils.messages.create_system_api_error_message` payload) — yielded once per backoff
  chunk while waiting to retry.
- :class:`RetryResult` wraps the successful operation result (the anthropic ``Stream`` or
  ``BetaMessage``) — yielded **exactly once, as the LAST item**, on success.

Consumer (``model_client``)::

    async for item in with_retry(get_client, operation, options):
        if isinstance(item, RetryError):
            yield item.message          # surfaces as {type:'system', subtype:'api_error'}
        elif isinstance(item, RetryResult):
            stream = item.value
            break

The retried operation is an async callable ``operation(client, attempt, context)`` returning the
stream/message. :func:`with_retry` invokes it, catches retryable errors, yields a
:class:`RetryError` while it backs off, and yields :class:`RetryResult` on success.

Stub notes (sensible defaults):
- ``APIUserAbortError``: the anthropic Python SDK does not export this class, so a local
  :class:`APIUserAbortError` is defined (raised on signal abort).
- Stale-connection keep-alive disabling, feature-flag gating, proxy tagging, and the persistent
  retry mode (``TABVIS_UNATTENDED_RETRY``) are stubbed: persistent retry is wired to OFF
  (``is_persistent_retry_enabled`` returns ``False``). The fallback (Opus -> Sonnet on 3
  consecutive 529s) is present but unreachable by default (no ``fallback_model`` set,
  ``is_non_custom_opus_model`` gate).
- ``sleep`` is an inline abort-responsive ``asyncio.sleep``; it raises the supplied ``abort_error``
  factory result when the signal aborts.
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpcore
import httpx
from anthropic import APIConnectionError, APIError, APIStatusError

from tabvis.agent.api.errors import REPEATED_529_ERROR_MESSAGE
from tabvis.utils.abort import AbortSignal
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.log import log_error
from tabvis.utils.messages import create_system_api_error_message
from tabvis.utils.model.model import get_model_strings
from tabvis.utils.thinking import ThinkingConfig

__all__ = [
    "BASE_DELAY_MS",
    "CannotRetryError",
    "FallbackTriggeredError",
    "RetryContext",
    "RetryError",
    "RetryResult",
    "get_default_max_retries",
    "get_retry_delay",
    "is_529_error",
    "parse_max_tokens_context_overflow_error",
    "with_retry",
]


# --------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------

DEFAULT_MAX_RETRIES = 10
FLOOR_OUTPUT_TOKENS = 3000
MAX_529_RETRIES = 3
BASE_DELAY_MS = 500

PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000
PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000
HEARTBEAT_INTERVAL_MS = 30_000

# Foreground query sources where the user IS blocking on the result — these retry on 529.
# Everything else bails immediately (see withRetry.ts:34-53).
FOREGROUND_529_RETRY_SOURCES: frozenset[str] = frozenset(
    {
        "repl_main_thread",
        "repl_main_thread:outputStyle:custom",
        "repl_main_thread:outputStyle:Explanatory",
        "repl_main_thread:outputStyle:Learning",
        "sdk",
        "agent:custom",
        "agent:default",
        "agent:builtin",
        "compact",
        "hook_agent",
        "hook_prompt",
        "verification_agent",
        "side_question",
    }
)


# --------------------------------------------------------------------------------------------
# Tagged yield protocol (LOCKED — replaces the TS `'controller' in value` discriminator)
# --------------------------------------------------------------------------------------------


@dataclass
class RetryError:
    """A retryable-failure heartbeat: wraps a ``system``/``api_error`` sentinel dict."""

    message: dict[str, Any]


@dataclass
class RetryResult:
    """The successful operation result (anthropic ``Stream`` or ``BetaMessage``)."""

    value: Any


# --------------------------------------------------------------------------------------------
# Abort error (the anthropic Python SDK does not export APIUserAbortError)
# --------------------------------------------------------------------------------------------


class APIUserAbortError(Exception):
    """Raised when the request is aborted via ``options.signal``.

    The ``anthropic`` SDK does not export an abort error, so this build defines its own.
    """

    def __init__(self, message: str = "Request was aborted.") -> None:
        super().__init__(message)
        self.message = message


def _abort_error() -> APIUserAbortError:
    return APIUserAbortError()


# --------------------------------------------------------------------------------------------
# Retry context + custom errors (withRetry.ts:91-137)
# --------------------------------------------------------------------------------------------


@dataclass
class RetryContext:
    """Per-call mutable context threaded into ``operation`` (withRetry.ts:91)."""

    model: str
    thinking_config: ThinkingConfig
    max_tokens_override: int | None = None


@dataclass
class RetryOptions:
    """Options bag for :func:`with_retry` (withRetry.ts:97)."""

    model: str
    thinking_config: ThinkingConfig
    max_retries: int | None = None
    fallback_model: str | None = None
    signal: AbortSignal | None = None
    query_source: str | None = None
    # Pre-seed the consecutive 529 counter (streaming -> non-streaming fallback continuity).
    initial_consecutive_529_errors: int = 0


class CannotRetryError(Exception):
    """Error raised when a failed request cannot be retried.

    Note: the TS class sets ``this.name = 'RetryError'`` (intentional, in the oracle). We keep the
    original error + retry context as attributes; the Python ``__name__`` is the class name.
    """

    def __init__(self, original_error: Any, retry_context: RetryContext) -> None:
        super().__init__(_error_message(original_error))
        self.original_error = original_error
        self.retry_context = retry_context
        # Preserve the original traceback if available (TS: copy .stack).
        if isinstance(original_error, BaseException):
            self.__cause__ = original_error


class FallbackTriggeredError(Exception):
    """Signal that retry handling should switch to a fallback model."""

    def __init__(self, original_model: str, fallback_model: str) -> None:
        super().__init__(f"Model fallback triggered: {original_model} -> {fallback_model}")
        self.original_model = original_model
        self.fallback_model = fallback_model


# --------------------------------------------------------------------------------------------
# Error-shape accessors (mirror tabvis/services/api/errors.py: status_code / headers / message)
# --------------------------------------------------------------------------------------------


def _status_code(error: Any) -> int | None:
    """Map TS ``error.status`` → Python ``error.status_code`` (present on ``APIStatusError``)."""
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) else None


def _error_message(error: Any) -> str:
    """``.Message`` for Errors, else ``str``."""
    if isinstance(error, BaseException):
        msg = getattr(error, "message", None)
        if isinstance(msg, str):
            return msg
        return str(error)
    return str(error)


def _header(error: Any, name: str) -> str | None:
    """Map TS ``error.headers?.get?.(name)`` → ``error.response.headers.get(name)`` (guarded)."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------


def _get_api_provider_for_statsig() -> str:
    """Provider tag for analytics — first-party only in this build.

    Provider detection is not implemented; always returns first-party.
    """
    return "firstParty"


def _get_feature_value_cached(_flag: str, default: bool) -> bool:
    """Growthbook feature-flag read — not implemented in this build; returns the default."""
    return default


def _disable_keep_alive() -> None:
    """Proxy keep-alive toggle — no-op in this build (single-shot httpx client)."""


def _clear_api_key_helper_cache() -> None:
    """Clear the apiKeyHelper cache — no-op in this build (no such cache)."""


def _extract_connection_error_details(_error: APIConnectionError) -> dict[str, Any] | None:
    """Extract a connection-error code from the error.

    Best-effort: sniff the message text for the ECONNRESET/EPIPE codes so
    :func:`_is_stale_connection_error` can fire.
    """
    msg = _error_message(_error)
    for code in ("ECONNRESET", "EPIPE"):
        if code in msg:
            return {"code": code}
    return None


def is_persistent_retry_enabled() -> bool:
    """Return whether persistent retry enabled.

    The oracle is ``return false ? isEnvTruthy(TABVIS_UNATTENDED_RETRY) : false`` — i.e. hard OFF
    (the env read is dead behind a ``false`` build gate). Mirror that exactly.
    """
    #   return is_env_truthy(os.environ.get("TABVIS_UNATTENDED_RETRY"))
    return False


def is_non_custom_opus_model(model: str) -> bool:
    """Return whether non custom opus model.

    True iff ``model`` is one of the canonical first-party Opus strings (opus40/41/45/46).
    (Not yet exported from ``tabvis.utils.model.model`` — reconstructed from ``get_model_strings``.)
    """
    strings = get_model_strings()
    return model in {
        strings.get("opus40"),
        strings.get("opus41"),
        strings.get("opus45"),
        strings.get("opus46"),
    }


# --------------------------------------------------------------------------------------------
# Abort-responsive sleep (inline; utils/sleep.py not yet implemented)
# --------------------------------------------------------------------------------------------


async def _sleep(
    ms: float,
    signal: AbortSignal | None = None,
    *,
    abort_error: Callable[[], BaseException] | None = None,
) -> None:
    """Sleep for ``ms`` milliseconds, aborting promptly when signaled.

    Resolves after ``ms`` milliseconds, or raises ``abort_error()`` immediately when ``signal``
    aborts (so backoff loops don't block shutdown). Mirrors the TS ``abortError`` path which
    implies ``throwOnAbort``.
    """
    if signal is not None and signal.aborted:
        if abort_error is not None:
            raise abort_error()
        return

    if signal is None:
        await asyncio.sleep(ms / 1000)
        return

    # Race the timer against the abort signal.
    sleep_task = asyncio.ensure_future(asyncio.sleep(ms / 1000))
    abort_task = asyncio.ensure_future(signal.wait())
    try:
        done, pending = await asyncio.wait(
            {sleep_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (sleep_task, abort_task):
            if not task.done():
                task.cancel()
    if abort_task in done and signal.aborted:
        if abort_error is not None:
            raise abort_error()
    # else: the sleep completed normally.


# --------------------------------------------------------------------------------------------
# 529 classification + retry-source gating (withRetry.ts:55-89, 473-484)
# --------------------------------------------------------------------------------------------


def is_529_error(error: Any) -> bool:
    """Return whether an error represents HTTP 529 capacity exhaustion.

    True for a 529 status code, OR (because the SDK sometimes drops the 529 status during
    streaming) an error message containing ``"type":"overloaded_error"``.
    """
    if not isinstance(error, APIError):
        return False
    return _status_code(error) == 529 or "overloaded_error" in _error_message(error).lower()


def should_retry_529(query_source: str | None) -> bool:
    """Retry (conservative)."""
    return query_source is None or query_source in FOREGROUND_529_RETRY_SOURCES


def _is_transient_capacity_error(error: Any) -> bool:
    """Return whether transient capacity error."""
    return is_529_error(error) or (isinstance(error, APIError) and _status_code(error) == 429)


def _is_stale_connection_error(error: Any) -> bool:
    """Return whether stale connection error."""
    if not isinstance(error, APIConnectionError):
        return False
    details = _extract_connection_error_details(error)
    return bool(details) and details.get("code") in ("ECONNRESET", "EPIPE")


# Raw transport errors raised by httpx/httpcore *during stream iteration*. These are NOT wrapped in
# anthropic.APIError (the SDK only wraps errors that occur before/at the initial request, not those
# raised lazily while draining ``response.aiter_bytes()``), so they bypass the APIError-based retry
# path entirely. The canonical case is a mid-stream disconnect:
#   httpcore.RemoteProtocolError: peer closed connection without sending complete message body
#   -> httpx.RemoteProtocolError (incomplete chunked read)
# These are transient network blips and are safe to retry (a fresh request is sent on each attempt).
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.RemoteProtocolError,
    httpx.TransportError,  # covers ReadError/ReadTimeout/ConnectError/etc. + RemoteProtocolError
    httpcore.RemoteProtocolError,
    httpcore.ProtocolError,
)


def _is_retryable_transport_error(error: Any) -> bool:
    """True for raw httpx/httpcore transport errors (e.g. a mid-stream incomplete-read disconnect)
    that the anthropic SDK does not surface as an ``APIError``."""
    if isinstance(error, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    # The disconnect is sometimes chained (httpx wraps the httpcore cause) or only identifiable from
    # the message text ("incomplete chunked read" / "peer closed connection"). Sniff both.
    cause = getattr(error, "__cause__", None)
    if isinstance(cause, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    msg = _error_message(error).lower()
    return (
        "incomplete chunked read" in msg
        or "peer closed connection without sending complete message body" in msg
    )


# --------------------------------------------------------------------------------------------
# Retry-after / backoff math (withRetry.ts:395-424, 559-579)
# --------------------------------------------------------------------------------------------


def _get_retry_after(error: Any) -> str | None:
    """Reads the ``retry-after`` header."""
    headers = getattr(error, "headers", None)
    if isinstance(headers, dict):
        value = headers.get("retry-after")
        if isinstance(value, str):
            return value
    return _header(error, "retry-after")


def get_retry_delay(
    attempt: int,
    retry_after_header: str | None = None,
    max_delay_ms: float = 32000,
) -> float:
    """Exponential backoff with 25% jitter.

    A numeric ``retry-after`` header overrides the computed delay (seconds → ms) and bypasses
    ``max_delay_ms`` (a server directive — honoring it is correct).
    """
    if retry_after_header:
        try:
            seconds = int(retry_after_header)
            return seconds * 1000
        except ValueError:
            pass

    base_delay = min(BASE_DELAY_MS * (2 ** (attempt - 1)), max_delay_ms)
    jitter = random.random() * 0.25 * base_delay
    return base_delay + jitter


def get_default_max_retries() -> int:
    """Return the default max retries."""
    raw = os.environ.get("TABVIS_MAX_RETRIES")
    if raw:
        try:
            return int(raw)
        except ValueError:
            return DEFAULT_MAX_RETRIES
    return DEFAULT_MAX_RETRIES


def _get_max_retries(options: RetryOptions) -> int:
    """Return the max retries."""
    return options.max_retries if options.max_retries is not None else get_default_max_retries()


def _get_rate_limit_reset_delay_ms(error: APIError) -> float | None:
    """Return the rate limit reset delay ms.

    Window-based limits include an absolute reset timestamp; wait until reset rather than polling.
    """
    reset_header = _header(error, "anthropic-ratelimit-unified-reset")
    if not reset_header:
        return None
    try:
        reset_unix_sec = float(reset_header)
    except ValueError:
        return None
    if reset_unix_sec != reset_unix_sec or reset_unix_sec in (float("inf"), float("-inf")):
        return None
    delay_ms = reset_unix_sec * 1000 - datetime.now(UTC).timestamp() * 1000
    if delay_ms <= 0:
        return None
    return min(delay_ms, PERSISTENT_RESET_CAP_MS)


# --------------------------------------------------------------------------------------------
# Max-tokens context-overflow parsing (withRetry.ts:426-471)
# --------------------------------------------------------------------------------------------

_OVERFLOW_RE = re.compile(
    r"input length and `max_tokens` exceed context limit: (\d+) \+ (\d+) > (\d+)"
)


def parse_max_tokens_context_overflow_error(error: APIError) -> dict[str, int] | None:
    """Parse the max tokens context overflow error.

    Returns ``{'input_tokens', 'max_tokens', 'context_limit'}`` or ``None``. Only fires for 400s
    whose message contains the context-limit overflow string.
    """
    if _status_code(error) != 400:
        return None
    message = _error_message(error)
    if not message:
        return None
    if "input length and `max_tokens` exceed context limit" not in message:
        return None
    match = _OVERFLOW_RE.search(message)
    if not match:
        return None
    if not (match.group(1) and match.group(2) and match.group(3)):
        log_error(
            ValueError(
                "Unable to parse max_tokens from max_tokens exceed context limit error message"
            )
        )
        return None
    try:
        input_tokens = int(match.group(1))
        max_tokens = int(match.group(2))
        context_limit = int(match.group(3))
    except ValueError:
        return None
    return {
        "input_tokens": input_tokens,
        "max_tokens": max_tokens,
        "context_limit": context_limit,
    }


# --------------------------------------------------------------------------------------------
# shouldRetry (withRetry.ts:486-557)
# --------------------------------------------------------------------------------------------


def _should_retry(error: APIError) -> bool:
    """Return whether retry should apply."""
    # Persistent mode: 429/529 always retryable (bypass gates + x-should-retry).
    if is_persistent_retry_enabled() and _is_transient_capacity_error(error):
        return True

    # CCR mode: 401/403 is a transient infra blip (JWT auth), not bad creds.
    if is_env_truthy(os.environ.get("TABVIS_REMOTE")) and _status_code(error) in (401, 403):
        return True

    # Overloaded errors (SDK sometimes drops the 529 status during streaming). The SDK builds the
    # streamed-error message from a Python dict repr (single quotes), so match the bare substring.
    if "overloaded_error" in _error_message(error).lower():
        return True

    # Mid-stream SSE `event: error` on an already-opened stream: the anthropic SDK raises an
    # APIStatusError carrying the *stream's* HTTP status — a 2xx (normally 200) — so it matches none
    # of the 4xx/5xx branches below and would fall through to `return False` -> CannotRetryError ->
    # crash the headless run. GLM/bigmodel commonly signals capacity/rate-limit AFTER the 200 stream
    # opens this way. Treat any 2xx-status APIStatusError as a transient mid-stream error and retry
    # (still bounded by max_retries). Placed before the x-should-retry:false early-return below so it
    # cannot be short-circuited.
    _mid_status = _status_code(error)
    if (
        isinstance(error, APIStatusError)
        and _mid_status is not None
        and 200 <= _mid_status < 300
    ):
        return True

    # Max-tokens context overflow errors we can handle by adjusting max_tokens.
    if parse_max_tokens_context_overflow_error(error):
        return True

    # Non-standard header: obey an explicit server directive.
    should_retry_header = _header(error, "x-should-retry")
    if should_retry_header == "true":
        return True

    if should_retry_header == "false":
        return False

    if isinstance(error, APIConnectionError):
        return True

    status = _status_code(error)
    if not status:
        return False

    if status == 408:  # request timeouts
        return True
    if status == 409:  # lock timeouts
        return True
    if status == 429:
        return True
    if status == 401:  # clear API key cache + allow retry
        _clear_api_key_helper_cache()
        return True
    if status >= 500:  # internal errors
        return True
    return False


# --------------------------------------------------------------------------------------------
# with_retry (withRetry.ts:139-393)
# --------------------------------------------------------------------------------------------


async def with_retry(
    get_client: Callable[[], Awaitable[Any]],
    operation: Callable[[Any, int, RetryContext], Awaitable[Any]],
    options: RetryOptions,
):
    """LOCKED async-generator protocol.

    Yields :class:`RetryError` items (retry heartbeats) and finally a single :class:`RetryResult`
    on success. On unrecoverable failure raises :class:`CannotRetryError` /
    :class:`FallbackTriggeredError` / :class:`APIUserAbortError` (never yields a result).
    """
    max_retries = _get_max_retries(options)
    retry_context = RetryContext(model=options.model, thinking_config=options.thinking_config)
    client: Any = None
    consecutive_529_errors = options.initial_consecutive_529_errors
    last_error: Any = None
    persistent_attempt = 0

    attempt = 1
    while attempt <= max_retries + 1:
        if options.signal is not None and options.signal.aborted:
            raise APIUserAbortError()

        try:
            # Fresh client on first attempt or after auth/stale-connection errors.
            is_stale_connection = _is_stale_connection_error(last_error)
            if is_stale_connection and _get_feature_value_cached(
                "tengu_disable_keepalive_on_econnreset", False
            ):
                log_for_debugging(
                    "Stale connection (ECONNRESET/EPIPE) — disabling keep-alive for retry"
                )
                _disable_keep_alive()

            if (
                client is None
                or (isinstance(last_error, APIError) and _status_code(last_error) == 401)
                or is_stale_connection
            ):
                client = await get_client()

            result = await operation(client, attempt, retry_context)
            yield RetryResult(value=result)
            return
        except (
            CannotRetryError,
            FallbackTriggeredError,
            APIUserAbortError,
            GeneratorExit,
            asyncio.CancelledError,
        ):
            # GeneratorExit/CancelledError arrive when the consumer breaks out of the
            # `async for` after RetryResult (aclose throws into the suspended yield). They are
            # clean shutdown, NOT retryable failures — must propagate, never re-classify.
            raise
        except Exception as error:  # noqa: BLE001 - the oracle catches all + re-classifies
            last_error = error
            log_for_debugging(
                f"API error (attempt {attempt}/{max_retries + 1}): "
                + (
                    f"{_status_code(error)} {_error_message(error)}"
                    if isinstance(error, APIError)
                    else _error_message(error)
                ),
                {"level": "error"},
            )

            # Non-foreground sources bail immediately on 529 (no amplification).
            if is_529_error(error) and not should_retry_529(options.query_source):
                raise CannotRetryError(error, retry_context) from error

            # Track consecutive 529 errors (fallback gate).
            if is_529_error(error) and (
                os.environ.get("FALLBACK_FOR_ALL_PRIMARY_MODELS")
                or is_non_custom_opus_model(options.model)
            ):
                consecutive_529_errors += 1
                if consecutive_529_errors >= MAX_529_RETRIES:
                    if options.fallback_model:
                        raise FallbackTriggeredError(
                            options.model, options.fallback_model
                        ) from error

                    if (
                        os.environ.get("USER_TYPE") == "external"
                        and not os.environ.get("IS_SANDBOX")
                        and not is_persistent_retry_enabled()
                    ):
                        raise CannotRetryError(
                            Exception(REPEATED_529_ERROR_MESSAGE), retry_context
                        ) from error

            # Only retry if the error indicates we should.
            persistent = is_persistent_retry_enabled() and _is_transient_capacity_error(error)
            if attempt > max_retries and not persistent:
                raise CannotRetryError(error, retry_context) from error

            # Raw httpx/httpcore transport errors (notably mid-stream incomplete-read disconnects)
            # are not anthropic.APIError instances, so they would otherwise fail the APIError gate
            # below and raise CannotRetryError. They are transient — retry them.
            if not isinstance(error, APIError):
                if not _is_retryable_transport_error(error):
                    raise CannotRetryError(error, retry_context) from error
            elif not _should_retry(error):
                raise CannotRetryError(error, retry_context) from error

            # Max-tokens context overflow: adjust max_tokens for the next attempt + continue.
            overflow_data = parse_max_tokens_context_overflow_error(error)
            if overflow_data:
                input_tokens = overflow_data["input_tokens"]
                context_limit = overflow_data["context_limit"]
                safety_buffer = 1000
                available_context = max(0, context_limit - input_tokens - safety_buffer)
                if available_context < FLOOR_OUTPUT_TOKENS:
                    log_error(
                        ValueError(
                            f"availableContext {available_context} is less than "
                            f"FLOOR_OUTPUT_TOKENS {FLOOR_OUTPUT_TOKENS}"
                        )
                    )
                    raise error
                thinking = retry_context.thinking_config
                min_required = (
                    (thinking.get("budgetTokens", 0) if thinking.get("type") == "enabled" else 0)
                    + 1
                )
                adjusted_max_tokens = max(FLOOR_OUTPUT_TOKENS, available_context, min_required)
                retry_context.max_tokens_override = adjusted_max_tokens
                attempt += 1
                continue

            # Normal retry: compute the delay.
            retry_after = _get_retry_after(error)
            if persistent and isinstance(error, APIError) and _status_code(error) == 429:
                persistent_attempt += 1
                reset_delay = _get_rate_limit_reset_delay_ms(error)
                delay_ms = (
                    reset_delay
                    if reset_delay is not None
                    else min(
                        get_retry_delay(
                            persistent_attempt, retry_after, PERSISTENT_MAX_BACKOFF_MS
                        ),
                        PERSISTENT_RESET_CAP_MS,
                    )
                )
            elif persistent:
                persistent_attempt += 1
                delay_ms = min(
                    get_retry_delay(persistent_attempt, retry_after, PERSISTENT_MAX_BACKOFF_MS),
                    PERSISTENT_RESET_CAP_MS,
                )
            else:
                delay_ms = get_retry_delay(attempt, retry_after)

            reported_attempt = persistent_attempt if persistent else attempt

            if persistent:
                if delay_ms > 60_000:
                    pass
                # Chunk long sleeps so the host sees periodic activity (keep-alive heartbeats).
                remaining = delay_ms
                while remaining > 0:
                    if options.signal is not None and options.signal.aborted:
                        raise APIUserAbortError() from error
                    if isinstance(error, APIError):
                        yield RetryError(
                            message=create_system_api_error_message(
                                error, remaining, reported_attempt, max_retries
                            )
                        )
                    chunk = min(remaining, HEARTBEAT_INTERVAL_MS)
                    await _sleep(chunk, options.signal, abort_error=_abort_error)
                    remaining -= chunk
                # Clamp so the loop never terminates (persistent_attempt drives backoff).
                if attempt >= max_retries:
                    attempt = max_retries
            else:
                if isinstance(error, APIError):
                    yield RetryError(
                        message=create_system_api_error_message(
                            error, delay_ms, attempt, max_retries
                        )
                    )
                await _sleep(delay_ms, options.signal, abort_error=_abort_error)

        attempt += 1

    raise CannotRetryError(last_error, retry_context)
