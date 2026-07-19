"""Context-window token accounting from transcript messages.

These functions read the usage data stamped on
assistant messages by the streaming loop and compute the current context-window
size, optionally estimating messages added since the last API response.

Transcript messages are plain ``dict`` envelopes (camelCase wire keys; inner
Anthropic blocks keep snake_case), per ``docs/SPINE_CONTRACTS.md``.

CYCLE: part of the ``context-tokens`` cluster. The only cycle sibling used here
(:mod:`tabvis.services.token_estimation`) is imported function-locally so this
module imports standalone.
"""

from __future__ import annotations

from typing import Any

from tabvis.utils.messages import SYNTHETIC_MODEL
from tabvis.utils.selectable_messages import SYNTHETIC_MESSAGES
from tabvis.utils.slow_operations import json_stringify

Message = dict[str, Any]
Usage = dict[str, Any]


def _rough_token_count_estimation_for_messages(messages: list[Message]) -> int:
    """Lazy bridge to the cycle sibling ``token_estimation``."""
    from tabvis.services.token_estimation import (
        rough_token_count_estimation_for_messages,
    )

    return rough_token_count_estimation_for_messages(messages)


def get_token_usage(message: Message) -> Usage | None:
    """Return the API usage dict for a real (non-synthetic) assistant message."""
    if message and message.get("type") == "assistant":
        inner = message.get("message", {})
        if "usage" not in inner:
            return None
        content = inner.get("content") or []
        first = content[0] if content else None
        is_synthetic_text = (
            isinstance(first, dict)
            and first.get("type") == "text"
            and first.get("text") in SYNTHETIC_MESSAGES
        )
        if not is_synthetic_text and inner.get("model") != SYNTHETIC_MODEL:
            return inner.get("usage")
    return None


def _get_assistant_message_id(message: Message) -> str | None:
    """API response id for an assistant message with real (non-synthetic) usage.

    Used to identify split assistant records from the same API response — parallel
    tool calls stream as separate AssistantMessage records sharing one ``message.id``.
    """
    if message and message.get("type") == "assistant":
        inner = message.get("message", {})
        if "id" in inner and inner.get("model") != SYNTHETIC_MODEL:
            return inner.get("id")
    return None


def get_token_count_from_usage(usage: Usage) -> int:
    """Total context-window tokens for an API response (input + cache + output)."""
    return (
        usage.get("input_tokens", 0)
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + usage.get("output_tokens", 0)
    )


def token_count_from_last_api_response(messages: list[Message]) -> int:
    """Total context tokens from the most recent usage-bearing message."""
    i = len(messages) - 1
    while i >= 0:
        message = messages[i]
        usage = get_token_usage(message) if message else None
        if usage:
            return get_token_count_from_usage(usage)
        i -= 1
    return 0


def final_context_tokens_from_last_response(messages: list[Message]) -> int:
    """Final context-window size from the last response's ``usage.iterations[-1]``.

    Used for ``task_budget.remaining`` across compaction boundaries. Falls back to
    top-level ``input_tokens + output_tokens`` (no cache) when ``iterations`` is
    absent. Both paths exclude cache tokens to match #304930's formula.
    """
    i = len(messages) - 1
    while i >= 0:
        message = messages[i]
        usage = get_token_usage(message) if message else None
        if usage:
            iterations = usage.get("iterations")
            if iterations and len(iterations) > 0:
                last = iterations[-1]
                return last.get("input_tokens", 0) + last.get("output_tokens", 0)
            # No iterations → no server tool loop → top-level usage IS the final
            # window. Match the iterations-path formula (input + output, no cache).
            return usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        i -= 1
    return 0


def message_token_count_from_last_api_response(messages: list[Message]) -> int:
    """Only the ``output_tokens`` from the last API response.

    WARNING: do NOT use for threshold comparisons (autocompact, session memory) —
    use :func:`token_count_with_estimation` which measures full context size.
    """
    i = len(messages) - 1
    while i >= 0:
        message = messages[i]
        usage = get_token_usage(message) if message else None
        if usage:
            return usage.get("output_tokens", 0)
        i -= 1
    return 0


def get_current_usage(messages: list[Message]) -> dict[str, int] | None:
    """Return the most recent real usage as a 4-field dict, or ``None``."""
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        usage = get_token_usage(message) if message else None
        if usage:
            return {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get(
                    "cache_creation_input_tokens"
                )
                or 0,
                "cache_read_input_tokens": usage.get("cache_read_input_tokens")
                or 0,
            }
    return None


def does_most_recent_assistant_message_exceed_200k(messages: list[Message]) -> bool:
    """True if the last assistant message's context window exceeds 200k tokens."""
    threshold = 200_000

    last_asst = None
    for message in reversed(messages):
        if message.get("type") == "assistant":
            last_asst = message
            break
    if not last_asst:
        return False
    usage = get_token_usage(last_asst)
    return get_token_count_from_usage(usage) > threshold if usage else False


def get_assistant_message_content_length(message: Message) -> int:
    """Character content length of an assistant message (chars/4 ≈ tokens).

    Counts the same content ``handleMessageFromStream`` counts via deltas: text,
    thinking, redacted_thinking data, and tool_use input. ``signature_delta`` is
    excluded (not model output).
    """
    content_length = 0
    for block in message.get("message", {}).get("content", []):
        block_type = block.get("type") if isinstance(block, dict) else None
        if block_type == "text":
            content_length += len(block.get("text", ""))
        elif block_type == "thinking":
            content_length += len(block.get("thinking", ""))
        elif block_type == "redacted_thinking":
            content_length += len(block.get("data", ""))
        elif block_type == "tool_use":
            content_length += len(json_stringify(block.get("input")))
    return content_length


def token_count_with_estimation(messages: list[Message]) -> int:
    """CANONICAL current context-window size (last API usage + estimate of newer).

    Always use this for threshold checks (autocompact, session memory). Walks back
    past split sibling records sharing the same ``message.id`` so interleaved
    tool_results between parallel-tool-call splits are included in the estimate.
    """
    i = len(messages) - 1
    while i >= 0:
        message = messages[i]
        usage = get_token_usage(message) if message else None
        if message and usage:
            # Walk back past earlier sibling records split from the same API
            # response (same message.id) so interleaved tool_results are counted.
            response_id = _get_assistant_message_id(message)
            if response_id:
                j = i - 1
                while j >= 0:
                    prior = messages[j]
                    prior_id = _get_assistant_message_id(prior) if prior else None
                    if prior_id == response_id:
                        # Earlier split of the same API response — anchor here.
                        i = j
                    elif prior_id is not None:
                        # Hit a different API response — stop walking.
                        break
                    # prior_id is None: user/tool_result/attachment — keep walking.
                    j -= 1
            return get_token_count_from_usage(
                usage
            ) + _rough_token_count_estimation_for_messages(messages[i + 1 :])
        i -= 1
    return _rough_token_count_estimation_for_messages(messages)
