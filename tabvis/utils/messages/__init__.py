"""Message builders + API-normalization helpers

Scope (headless walking-skeleton): the message-envelope builders and the two API-prep
normalizers the spine needs. See ``docs/SPINE_CONTRACTS.md`` for the locked envelope shapes.

Implemented here:
- Constants: ``INTERRUPT_MESSAGE``, ``INTERRUPT_MESSAGE_FOR_TOOL_USE``, ``SYNTHETIC_MODEL``
  (and a re-export of ``NO_CONTENT_MESSAGE`` from ``tabvis.constants.messages``).
- Builders: ``base_create_assistant_message`` (internal), ``create_assistant_message``,
  ``create_assistant_api_error_message``, ``create_user_message``, ``create_progress_message``,
  ``create_system_api_error_message``.
- ``normalize_content_from_api`` (map content blocks; parse streamed tool_use JSON strings).
- ``normalize_messages_for_api`` (drop progress/non-local-command-system/virtual/synthetic-
  api-error messages; merge consecutive user turns; pass user/assistant through). PDF/image
  block-stripping and tool-search tool_reference stripping are not implemented in this build.

Messages + usage are PLAIN DICTS with the wire-key envelope shape (camelCase envelope keys,
snake_case inner Anthropic keys). NO pydantic — ``model_client`` mutates usage/stop_reason in
place after yielding.
"""

from __future__ import annotations

import json
import uuid as uuid_module
from datetime import UTC, datetime
from typing import Any

from tabvis.constants.messages import NO_CONTENT_MESSAGE
from tabvis.tool import Tool, Tools, find_tool_by_name
from tabvis.types.message import AssistantMessage, Message, ProgressMessage, UserMessage
from tabvis.utils.log import log_error

# --------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------

INTERRUPT_MESSAGE = "[Request interrupted by user]"
INTERRUPT_MESSAGE_FOR_TOOL_USE = "[Request interrupted by user for tool use]"

# Sentinel model id stamped onto locally-synthesized assistant messages (never from the API).
SYNTHETIC_MODEL = "<synthetic>"

__all__ = [
    "INTERRUPT_MESSAGE",
    "INTERRUPT_MESSAGE_FOR_TOOL_USE",
    "NO_CONTENT_MESSAGE",
    "SYNTHETIC_MODEL",
    "base_create_assistant_message",
    "create_assistant_api_error_message",
    "create_assistant_message",
    "create_progress_message",
    "create_system_api_error_message",
    "create_user_message",
    "get_assistant_message_text",
    "normalize_content_from_api",
    "normalize_messages_for_api",
]


# --------------------------------------------------------------------------------------------
# Small local helpers (stubbed deeper deps)
# --------------------------------------------------------------------------------------------


def get_assistant_message_text(message: Any) -> str:
    """Join the text blocks of an assistant message's content.

    Accepts an AssistantMessage envelope (``{"message": {"content": ...}}``). String content is
    returned verbatim; a content-block list contributes its ``text`` blocks (newline-joined);
    anything else yields ``""``.
    """
    if not isinstance(message, dict):
        return ""
    content = (message.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _utc_iso() -> str:
    """``new Date().toISOString()`` equivalent (UTC, ISO-8601)."""
    return datetime.now(UTC).isoformat()


def _new_uuid() -> str:
    """``randomUUID()`` equivalent."""
    return str(uuid_module.uuid4())


def safe_parse_json(text: str) -> Any | None:
    """``Json.loads`` returning ``None`` on failure."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _is_object(value: Any) -> bool:
    """lodash ``isObject`` semantics: dict or list (objects/arrays), not scalars/None."""
    return isinstance(value, (dict, list))


def _sanitize_tool_name_for_analytics(tool_name: str) -> str:
    """Redact MCP tool names for analytics."""
    if tool_name.startswith("mcp__"):
        return "mcp_tool"
    return tool_name


def _default_usage() -> dict[str, Any]:
    """The TS ``baseCreateAssistantMessage`` default usage object.

    NOTE: this intentionally differs from ``empty_usage()`` (which uses ``"standard"``/``""``/
    ``[]`` placeholders) — the TS default here uses ``None``/``null`` for the non-token fields.
    Kept faithful to the source so transcript round-trips match the oracle.
    """
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
        "service_tier": None,
        "cache_creation": {
            "ephemeral_1h_input_tokens": 0,
            "ephemeral_5m_input_tokens": 0,
        },
        "inference_geo": None,
        "iterations": None,
        "speed": None,
    }


# --------------------------------------------------------------------------------------------
# Assistant / user / progress builders
# --------------------------------------------------------------------------------------------


def base_create_assistant_message(
    *,
    content: list[dict[str, Any]],
    is_api_error_message: bool = False,
    api_error: str | None = None,
    error: str | None = None,
    error_details: str | None = None,
    is_virtual: bool | None = None,
    usage: dict[str, Any] | None = None,
) -> AssistantMessage:
    """Internal builder (TS ``baseCreateAssistantMessage``).

    Returns an AssistantMessage envelope (plain dict). Envelope keys are camelCase wire keys;
    the inner ``message`` keeps Anthropic snake_case keys.
    """
    if usage is None:
        usage = _default_usage()
    message: AssistantMessage = {
        "type": "assistant",
        "uuid": _new_uuid(),
        "timestamp": _utc_iso(),
        "message": {
            "id": _new_uuid(),
            "container": None,
            "model": SYNTHETIC_MODEL,
            "role": "assistant",
            "stop_reason": "stop_sequence",
            "stop_sequence": "",
            "type": "message",
            "usage": usage,
            "content": content,
            "context_management": None,
        },
        "requestId": None,
        "apiError": api_error,
        "error": error,
        "errorDetails": error_details,
        "isApiErrorMessage": is_api_error_message,
        "isVirtual": is_virtual,
    }
    return message


def create_assistant_message(
    *,
    content: str | list[dict[str, Any]],
    usage: dict[str, Any] | None = None,
    is_virtual: bool | None = None,
) -> AssistantMessage:
    """String content becomes one text block."""
    if isinstance(content, str):
        normalized_content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": NO_CONTENT_MESSAGE if content == "" else content,
            }
        ]
    else:
        normalized_content = content
    return base_create_assistant_message(
        content=normalized_content,
        usage=usage,
        is_virtual=is_virtual,
    )


def create_assistant_api_error_message(
    *,
    content: str,
    api_error: str | None = None,
    error: str | None = None,
    error_details: str | None = None,
) -> AssistantMessage:
    """Sets ``isApiErrorMessage=True``."""
    return base_create_assistant_message(
        content=[
            {
                "type": "text",
                "text": NO_CONTENT_MESSAGE if content == "" else content,
            }
        ],
        is_api_error_message=True,
        api_error=api_error,
        error=error,
        error_details=error_details,
    )


def create_user_message(
    *,
    content: str | list[dict[str, Any]],
    is_meta: bool | None = None,
    is_visible_in_transcript_only: bool | None = None,
    is_virtual: bool | None = None,
    is_compact_summary: bool | None = None,
    summarize_metadata: dict[str, Any] | None = None,
    tool_use_result: Any | None = None,
    mcp_meta: dict[str, Any] | None = None,
    uuid: str | None = None,
    timestamp: str | None = None,
    image_paste_ids: list[int] | None = None,
    source_tool_assistant_uuid: str | None = None,
    permission_mode: str | None = None,
    origin: Any | None = None,
) -> UserMessage:
    """Create the user message.

    Returns a UserMessage envelope (plain dict). Empty content collapses to
    ``NO_CONTENT_MESSAGE`` (the API rejects empty messages). Envelope keys are camelCase wire
    keys; the inner ``message`` keeps the Anthropic snake_case ``role``/``content`` keys.
    """
    message: UserMessage = {
        "type": "user",
        "message": {
            "role": "user",
            # Make sure we don't send empty messages.
            "content": content if content else NO_CONTENT_MESSAGE,
        },
        "isMeta": is_meta,
        "isVisibleInTranscriptOnly": is_visible_in_transcript_only,
        "isVirtual": is_virtual,
        "isCompactSummary": is_compact_summary,
        "summarizeMetadata": summarize_metadata,
        "uuid": uuid or _new_uuid(),
        "timestamp": timestamp if timestamp is not None else _utc_iso(),
        "toolUseResult": tool_use_result,
        "mcpMeta": mcp_meta,
        "imagePasteIds": image_paste_ids,
        "sourceToolAssistantUUID": source_tool_assistant_uuid,
        "permissionMode": permission_mode,
        "origin": origin,
    }
    return message


def create_progress_message(
    *,
    tool_use_id: str,
    parent_tool_use_id: str,
    data: Any,
) -> ProgressMessage:
    """Create the progress message."""
    message: ProgressMessage = {
        "type": "progress",
        "data": data,
        "toolUseID": tool_use_id,
        "parentToolUseID": parent_tool_use_id,
        "uuid": _new_uuid(),
        "timestamp": _utc_iso(),
    }
    return message


def create_system_api_error_message(
    error: Any,
    retry_in_ms: int,
    retry_attempt: int,
    max_retries: int,
) -> dict[str, Any]:
    """The system retry-heartbeat sentinel.

    NO ``message:str`` field. Dropped at the SDK boundary (QueryEngine has no ``system`` case).
    """
    cause = getattr(error, "cause", None)
    return {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "cause": cause if isinstance(cause, Exception) else None,
        "error": error,
        "retryInMs": retry_in_ms,
        "retryAttempt": retry_attempt,
        "maxRetries": max_retries,
        "timestamp": _utc_iso(),
        "uuid": _new_uuid(),
    }


# --------------------------------------------------------------------------------------------
# API content normalization
# --------------------------------------------------------------------------------------------


def _normalize_tool_input(
    tool: Tool,
    input_dict: dict[str, Any],
    agent_id: str | None = None,
) -> dict[str, Any]:
    """``normalizeToolInput``: identity unless the tool backfills observable input.

    This build applies only the tool's ``backfill_observable_input`` (which mutates a dict in
    place) on a copy. Per-tool corrections (Bash ``cd`` stripping, FileEdit/Write normalization,
    ExitPlanMode plan injection, TaskOutput legacy params) are not implemented in this build.
    """
    out = dict(input_dict)
    tool.backfill_observable_input(out)
    return out


def normalize_content_from_api(
    content_blocks: list[dict[str, Any]] | None,
    tools: Tools,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize the content from api.

    Maps API content blocks: for ``tool_use`` parse the streamed JSON-string ``input`` via
    ``safe_parse_json`` (-> ``{}`` on fail) then apply ``_normalize_tool_input``; ``server_tool_use``
    likewise parses a string input; ``text``/other blocks pass through unchanged.
    """
    if not content_blocks:
        return []

    result: list[dict[str, Any]] = []
    for content_block in content_blocks:
        block_type = content_block.get("type")

        if block_type == "tool_use":
            raw_input = content_block.get("input")
            if not isinstance(raw_input, str) and not _is_object(raw_input):
                # We stream tool use inputs as strings, but when we fall back, they're objects.
                raise ValueError("Tool use input must be a string or object")

            normalized_input: Any
            if isinstance(raw_input, str):
                parsed = safe_parse_json(raw_input)
                if parsed is None and len(raw_input) > 0:
                    # The streamed tool input JSON failed to parse; fall back to {}.
                    pass
                normalized_input = parsed if parsed is not None else {}
            else:
                normalized_input = raw_input

            # Then apply tool-specific corrections.
            if isinstance(normalized_input, dict):
                tool = find_tool_by_name(tools, content_block.get("name", ""))
                if tool is not None:
                    try:
                        normalized_input = _normalize_tool_input(
                            tool, normalized_input, agent_id
                        )
                    except Exception as exc:  # noqa: BLE001
                        log_error(Exception("Error normalizing tool input: " + str(exc)))
                        # Keep the original input if normalization fails.

            result.append({**content_block, "input": normalized_input})
            continue

        if block_type == "text":
            text = content_block.get("text", "")
            if len(text.strip()) == 0:
                pass
            # Return the block as-is to preserve exact content for prompt caching.
            result.append(content_block)
            continue

        if block_type in (
            "code_execution_tool_result",
            "mcp_tool_use",
            "mcp_tool_result",
            "container_upload",
        ):
            # Beta-specific content blocks — pass through as-is.
            result.append(content_block)
            continue

        if block_type == "server_tool_use":
            server_input = content_block.get("input")
            if isinstance(server_input, str):
                parsed_server = safe_parse_json(server_input)
                result.append(
                    {
                        **content_block,
                        "input": parsed_server if parsed_server is not None else {},
                    }
                )
                continue
            result.append(content_block)
            continue

        result.append(content_block)

    return result


# --------------------------------------------------------------------------------------------
# normalize_messages_for_api — merge + drop logic
# --------------------------------------------------------------------------------------------


def _is_synthetic_api_error_message(message: Message) -> bool:
    """TS ``isSyntheticApiErrorMessage``: an assistant API-error stamped with SYNTHETIC_MODEL."""
    return (
        message.get("type") == "assistant"
        and message.get("isApiErrorMessage") is True
        and message.get("message", {}).get("model") == SYNTHETIC_MODEL
    )


def _is_system_local_command_message(message: Message) -> bool:
    """A system message carrying local-command output (kept; folded into a user turn).

    This build treats any system message with string ``content`` as a local-command message.
    """
    return message.get("type") == "system" and isinstance(message.get("content"), str)


def _normalize_user_text_content(
    content: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """String -> single text block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def _join_text_at_seam(
    a: list[dict[str, Any]],
    b: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append ``\\n`` to a's last text block at a text-text seam.

    The API concatenates adjacent text blocks without a separator, so two queued prompts would
    otherwise be glued together. The ``\\n`` goes on a's side so no block's ``startswith`` changes.
    """
    last_a = a[-1] if a else None
    first_b = b[0] if b else None
    if (
        last_a is not None
        and last_a.get("type") == "text"
        and first_b is not None
        and first_b.get("type") == "text"
    ):
        return [
            *a[:-1],
            {**last_a, "text": last_a.get("text", "") + "\n"},
            *b,
        ]
    return [*a, *b]


def _hoist_tool_results(
    content: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Tool_result blocks must come first in a user turn."""
    tool_results: list[dict[str, Any]] = []
    other_blocks: list[dict[str, Any]] = []
    for block in content:
        if block.get("type") == "tool_result":
            tool_results.append(block)
        else:
            other_blocks.append(block)
    return [*tool_results, *other_blocks]


def merge_user_messages(a: UserMessage, b: UserMessage) -> UserMessage:
    """Merge consecutive user messages while preserving content order.

    Merges two consecutive user turns into one, joining text at the seam and hoisting tool
    results to the front. Preserves the non-meta message's uuid so derived ``[id:]`` tags stay
    stable across API calls.
    """
    last_content = _normalize_user_text_content(a.get("message", {}).get("content", ""))
    current_content = _normalize_user_text_content(
        b.get("message", {}).get("content", "")
    )
    merged: UserMessage = {
        **a,
        # Preserve the non-meta message's uuid.
        "uuid": b.get("uuid") if a.get("isMeta") else a.get("uuid"),
        "message": {
            **a.get("message", {}),
            "content": _hoist_tool_results(
                _join_text_at_seam(last_content, current_content)
            ),
        },
    }
    return merged


def _assistant_content_blocks(message: Message) -> list[Any]:
    """Return an assistant message's content as a list of blocks (a bare string -> one text block)."""
    content = (message.get("message") or {}).get("content")
    if isinstance(content, list):
        return list(content)
    if content:
        return [{"type": "text", "text": content}]
    return []


def _merge_same_id_assistant(first: Message, second: Message) -> Message:
    """Concatenate two assistant messages that share a ``message.id`` into one, preserving block order.

    ``model_client`` yields ONE assistant message per ``content_block_stop``, so a single model turn
    becomes several messages sharing the same id (e.g. ``[thinking]`` then ``[tool_use]`` then
    ``[tool_use]``). They must be recombined into one assistant turn whose content lists the blocks in
    order — otherwise a ``thinking`` block is separated from the ``tool_use`` it belongs to, which a
    thinking model (e.g. DeepSeek's Anthropic-compatible endpoint) rejects with 400 "``content[].
    thinking`` in the thinking mode must be passed back to the API". ``second`` is the later split, so
    its usage/stop_reason (which model_client stamps onto the LAST split of a turn) wins.
    """
    merged_inner: dict[str, Any] = {**(first.get("message") or {})}
    second_inner = second.get("message") or {}
    for k in ("usage", "stop_reason", "stop_sequence"):
        if k in second_inner:
            merged_inner[k] = second_inner[k]
    merged_inner["content"] = [*_assistant_content_blocks(first), *_assistant_content_blocks(second)]
    return {**first, "message": merged_inner}


def normalize_messages_for_api(
    messages: list[Message],
    tools: Tools | None = None,
) -> list[Message]:
    """Normalize the messages for api.

    Drops progress / non-local-command system / virtual / synthetic-api-error messages, merges
    consecutive user messages into one user turn, and passes user/assistant through. Local-command
    system messages are converted to user messages (so the model can reference prior output) and
    merged into the running user turn.

    This build does not (1) reorder attachments, (2) strip PDF/image blocks from the meta user
    message that preceded a size error, (3) strip tool_search tool_reference blocks or inject a
    turn-boundary sibling, (4) normalize assistant tool inputs for the API, or (5) filter
    orphaned/trailing thinking + whitespace-only messages. Those edge cases are not supported.
    (Same-id assistant-message merging IS implemented below, so DeepSeek thinking blocks ride with
    their paired tool_use; see ``_merge_same_id_assistant``.)
    """
    if tools is None:
        tools = []

    # Strip virtual user/assistant messages — they're display-only and must never reach the API.
    reordered: list[Message] = [
        m
        for m in messages
        if not (m.get("type") in ("user", "assistant") and m.get("isVirtual"))
    ]

    # Drop progress, non-local-command system, and synthetic-api-error messages.
    filtered = [
        m
        for m in reordered
        if not (
            m.get("type") == "progress"
            or (
                m.get("type") == "system"
                and not _is_system_local_command_message(m)
            )
            or _is_synthetic_api_error_message(m)
        )
    ]

    result: list[Message] = []
    for message in filtered:
        msg_type = message.get("type")

        if msg_type == "system":
            # local_command system messages become user messages so the model can reference
            # previous command output in later turns.
            user_msg = create_user_message(
                content=message.get("content", ""),
                uuid=message.get("uuid"),
                timestamp=message.get("timestamp"),
            )
            last_message = result[-1] if result else None
            if last_message is not None and last_message.get("type") == "user":
                result[-1] = merge_user_messages(last_message, user_msg)
                continue
            result.append(user_msg)
            continue

        if msg_type == "user":
            normalized_message = message
            last_message = result[-1] if result else None
            if last_message is not None and last_message.get("type") == "user":
                result[-1] = merge_user_messages(last_message, normalized_message)
                continue
            result.append(normalized_message)
            continue

        if msg_type == "assistant":
            # Merge consecutive same-id assistant messages back into one turn (model_client splits a
            # turn into one message per content_block_stop). This keeps a `thinking` block paired with
            # the `tool_use` it precedes, which thinking models require — DeepSeek's Anthropic endpoint
            # 400s with "content[].thinking ... must be passed back" otherwise. Harmless for
            # non-thinking models (it just reconstructs the canonical single-message assistant turn).
            cur_id = (message.get("message") or {}).get("id")
            last_message = result[-1] if result else None
            if (
                cur_id is not None
                and last_message is not None
                and last_message.get("type") == "assistant"
                and (last_message.get("message") or {}).get("id") == cur_id
            ):
                result[-1] = _merge_same_id_assistant(last_message, message)
                continue
            result.append(message)
            continue

        # Any other message type (e.g. attachment) is passed through for the skeleton.
        result.append(message)

    return result
