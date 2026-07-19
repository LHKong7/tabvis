"""Token estimation helpers.

Two flavours of estimation live here:

* **API-backed** counts (:func:`count_tokens_with_api`,
  :func:`count_messages_tokens_with_api`, :func:`count_tokens_via_haiku_fallback`)
  — call the Anthropic ``countTokens`` / ``messages.create`` endpoints.
* **Rough char/4 estimation** (:func:`rough_token_count_estimation` and the
  ``*_for_message(s)`` helpers) — pure, network-free, used by the spinner and by
  context analysis to estimate messages added since the last API response.

CYCLE: this module is part of the ``context-tokens`` mutually-recursive cluster
(``tokens`` / ``analyze_context`` / ``tool_search`` / ``attachments`` / compact).
Every cross-cycle reference is broken with ``TYPE_CHECKING`` type-only imports and
function-local (lazy) runtime imports, so this module imports standalone even
before its siblings exist on disk.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tabvis.utils.slow_operations import json_stringify

if TYPE_CHECKING:  # type-only — no runtime cycle edge
    from tabvis.utils.attachments import Attachment

# Minimal values for token counting with thinking enabled.
# API constraint: max_tokens must be greater than thinking.budget_tokens.
TOKEN_COUNT_THINKING_BUDGET = 1024
TOKEN_COUNT_MAX_TOKENS = 2048


def _js_round(value: float) -> int:
    """JS ``Math.round`` semantics: round half toward +Infinity.

    Python's built-in ``round`` is banker's rounding (half-to-even); JS is
    half-up. Token counts are non-negative so half-up via ``floor(x + 0.5)``
    is exact for our inputs.
    """
    import math

    return math.floor(value + 0.5)


def _has_thinking_blocks(messages: list[dict[str, Any]]) -> bool:
    """Check if any assistant message contains a thinking/redacted_thinking block."""
    for message in messages:
        if message.get("role") == "assistant" and isinstance(
            message.get("content"), list
        ):
            for block in message["content"]:
                if (
                    isinstance(block, dict)
                    and "type" in block
                    and block["type"] in ("thinking", "redacted_thinking")
                ):
                    return True
    return False


def _strip_tool_search_fields_from_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strip tool-search beta fields before sending for token counting.

    Removes ``caller`` from ``tool_use`` blocks and ``tool_reference`` blocks from
    ``tool_result`` content. These fields are only valid with the tool-search beta
    header and will cause errors otherwise.
    """
    # Lazy cycle-sibling import (tool_search ↔ token_estimation).
    from tabvis.utils.tool_search import is_tool_reference_block

    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue

        normalized_content: list[Any] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                # Destructure to exclude extra fields like 'caller'.
                normalized_content.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id"),
                        "name": block.get("name"),
                        "input": block.get("input"),
                    }
                )
                continue

            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_result_content = block.get("content")
                if isinstance(tool_result_content, list):
                    filtered_content = [
                        c
                        for c in tool_result_content
                        if not is_tool_reference_block(c)
                    ]
                    if len(filtered_content) == 0:
                        normalized_content.append(
                            {
                                **block,
                                "content": [
                                    {"type": "text", "text": "[tool references]"}
                                ],
                            }
                        )
                        continue
                    if len(filtered_content) != len(tool_result_content):
                        normalized_content.append(
                            {**block, "content": filtered_content}
                        )
                        continue

            normalized_content.append(block)

        out.append({**message, "content": normalized_content})

    return out


async def count_tokens_with_api(content: str) -> int | None:
    """Count tokens for a single string via the token-counting API."""
    # Special case for empty content — API doesn't accept empty messages.
    if not content:
        return 0

    message: dict[str, Any] = {"role": "user", "content": content}
    return await count_messages_tokens_with_api([message], [])


async def count_messages_tokens_with_api(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> int | None:
    """Count input tokens for a message list + tool set via the API."""

    async def _do() -> int | None:
        # Lazy imports of deep deps (cycle-safe + tolerant of unported tail).
        from tabvis.agent.api.client import get_provider_client
        from tabvis.utils.log import log_error
        from tabvis.utils.model.model import (
            get_main_loop_model,
            normalize_model_string_for_api,
        )

        try:
            model = get_main_loop_model()
            betas: list[str] = []
            contains_thinking = _has_thinking_blocks(messages)

            model_client = await get_provider_client(
                max_retries=1,
                model=model,
                source="count_tokens",
            )

            params: dict[str, Any] = {
                "model": normalize_model_string_for_api(model),
                # When we pass tools and no messages, pass a dummy message to get
                # an accurate tool token count.
                "messages": messages
                if len(messages) > 0
                else [{"role": "user", "content": "foo"}],
                "tools": tools,
            }
            if len(betas) > 0:
                params["betas"] = betas
            if contains_thinking:
                params["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": TOKEN_COUNT_THINKING_BUDGET,
                }

            response = await model_client.beta.messages.count_tokens(**params)

            input_tokens = getattr(response, "input_tokens", None)
            if not isinstance(input_tokens, int):
                return None
            return input_tokens
        except Exception as error:  # noqa: BLE001 - match TS catch-all
            log_error(error)
            return None

    return await _do()


def rough_token_count_estimation(content: str, bytes_per_token: float = 4) -> int:
    """Estimate token count as ``round(len(content) / bytes_per_token)``."""
    return _js_round(len(content) / bytes_per_token)


def bytes_per_token_for_file_type(file_extension: str) -> float:
    """Estimated bytes-per-token ratio for a file extension.

    Dense JSON has many single-character tokens (``{``, ``}``, ``:``, ``,``, ``"``)
    so the real ratio is closer to 2 than the default 4.
    """
    if file_extension in ("json", "jsonl", "jsonc"):
        return 2
    return 4


def rough_token_count_estimation_for_file_type(
    content: str, file_extension: str
) -> int:
    """Like :func:`rough_token_count_estimation` but file-type aware."""
    return rough_token_count_estimation(
        content, bytes_per_token_for_file_type(file_extension)
    )


async def count_tokens_via_haiku_fallback(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> int | None:
    """Estimate token count via a 1-token Haiku request when countTokens fails.

    Returns ``input_tokens + cache_creation + cache_read`` from the response usage.
    """
    # Lazy imports of deep deps (cycle-safe).
    from tabvis.agent.api.client import get_provider_client
    from tabvis.utils.side_query import get_api_metadata

    contains_thinking = _has_thinking_blocks(messages)

    # Haiku 4.5 supports thinking blocks. tool_reference blocks are stripped via
    # _strip_tool_search_fields_from_messages() before sending.
    model = _get_small_fast_model()
    model_client = await get_provider_client(
        max_retries=1,
        model=model,
        source="count_tokens",
    )

    normalized_messages = _strip_tool_search_fields_from_messages(messages)
    messages_to_send = (
        normalized_messages
        if len(normalized_messages) > 0
        else [{"role": "user", "content": "count"}]
    )

    betas: list[str] = []

    from tabvis.utils.model.model import normalize_model_string_for_api

    params: dict[str, Any] = {
        "model": normalize_model_string_for_api(model),
        "max_tokens": TOKEN_COUNT_MAX_TOKENS if contains_thinking else 1,
        "messages": messages_to_send,
        "tools": tools if len(tools) > 0 else None,
        "metadata": get_api_metadata(),
        **_get_extra_body_params(),
    }
    if len(betas) > 0:
        params["betas"] = betas
    if contains_thinking:
        params["thinking"] = {
            "type": "enabled",
            "budget_tokens": TOKEN_COUNT_THINKING_BUDGET,
        }

    response = await model_client.beta.messages.create(**params)

    usage = response.usage
    input_tokens = usage.input_tokens
    cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0

    return input_tokens + cache_creation_tokens + cache_read_tokens


def _get_small_fast_model() -> str:
    """Resolve the small/fast model; fall back to the Haiku default.

    ``get_small_fast_model`` is not implemented in this build, so this resolves
    to ``get_default_haiku_model``.
    """
    from tabvis.utils.model import model as model_mod

    fn = getattr(model_mod, "get_small_fast_model", None)
    if fn is not None:
        return fn()
    return model_mod.get_default_haiku_model()


def _get_extra_body_params() -> dict[str, Any]:
    """Resolve extra body params from the model client.

    Not exported by the model client in this build; returns ``{}``.
    """
    try:
        from tabvis.agent.api import model_client as mc

        fn = getattr(mc, "get_extra_body_params", None)
        if fn is not None:
            return fn()
    except Exception:  # noqa: BLE001 - import-tolerant
        pass
    return {}


def rough_token_count_estimation_for_messages(
    messages: list[dict[str, Any]],
) -> int:
    """Sum rough estimation across a list of transcript-envelope messages."""
    total_tokens = 0
    for message in messages:
        total_tokens += rough_token_count_estimation_for_message(message)
    return total_tokens


def rough_token_count_estimation_for_message(message: dict[str, Any]) -> int:
    """Rough estimation for a single transcript-envelope message dict."""
    msg_type = message.get("type")
    inner = message.get("message")
    if (
        msg_type in ("assistant", "user")
        and isinstance(inner, dict)
        and inner.get("content") is not None
    ):
        return _rough_token_count_estimation_for_content(inner.get("content"))

    if msg_type == "attachment" and message.get("attachment") is not None:
        attachment: Attachment = message["attachment"]
        user_messages = _normalize_attachment_for_api(attachment)
        total = 0
        for user_msg in user_messages:
            total += _rough_token_count_estimation_for_content(
                user_msg.get("message", {}).get("content")
            )
        return total

    return 0


def _normalize_attachment_for_api(attachment: Any) -> list[dict[str, Any]]:
    """Normalize an attachment for the API.

    Attachment normalization is not implemented in this build; returns ``[]`` so
    the estimate is conservative rather than crashing.
    """
    return []


def _rough_token_count_estimation_for_content(content: Any) -> int:
    if not content:
        return 0
    if isinstance(content, str):
        return rough_token_count_estimation(content)
    total_tokens = 0
    for block in content:
        total_tokens += _rough_token_count_estimation_for_block(block)
    return total_tokens


def _rough_token_count_estimation_for_block(block: Any) -> int:
    if isinstance(block, str):
        return rough_token_count_estimation(block)

    block_type = block.get("type") if isinstance(block, dict) else None

    if block_type == "text":
        return rough_token_count_estimation(block.get("text", ""))
    if block_type in ("image", "document"):
        # tokens = (width px * height px)/750; images resized to max 2000x2000
        # (5333 tokens). Use a conservative estimate that matches microCompact's
        # IMAGE_MAX_TOKEN_SIZE to avoid underestimating. A base64 PDF must NOT
        # reach the jsonStringify catch-all (a 1MB PDF is ~1.33M base64 chars →
        # ~325k estimated tokens, vs the ~2000 the API actually charges).
        return 2000
    if block_type == "tool_result":
        return _rough_token_count_estimation_for_content(block.get("content"))
    if block_type == "tool_use":
        # input is model-generated JSON — arbitrarily large. Stringify once for
        # the char count; the API re-serializes anyway.
        return rough_token_count_estimation(
            str(block.get("name", "")) + json_stringify(block.get("input") or {})
        )
    if block_type == "thinking":
        return rough_token_count_estimation(block.get("thinking", ""))
    if block_type == "redacted_thinking":
        return rough_token_count_estimation(block.get("data", ""))
    # server_tool_use, web_search_tool_result, mcp_tool_use, etc. — text-like
    # payloads (no base64). Stringify-length tracks the serialized form the API
    # sees; key/bracket overhead is single-digit percent on real blocks.
    return rough_token_count_estimation(json_stringify(block))
