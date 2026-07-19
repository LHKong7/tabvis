"""Microcompact — client-side tool-result compaction helpers.

Cycle note: this module is an *enabler* for the compact cycle. The only
cross-cycle runtime reference is ``rough_token_count_estimation`` (from
``services/token_estimation``), imported lazily inside the helpers that use it so
this module imports standalone even before its siblings exist on disk.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, TypedDict

from tabvis.agent.compact.compact_warning_state import (
    clear_compact_warning_suppression,
    suppress_compact_warning,
)
from tabvis.agent.compact.time_based_mc_config import (
    TimeBasedMCConfig,
    get_time_based_mc_config,
)
from tabvis.agent.tools.file_edit_tool import FILE_EDIT_TOOL_NAME, NOTEBOOK_EDIT_TOOL_NAME
from tabvis.agent.tools.file_read_tool import FILE_READ_TOOL_NAME
from tabvis.agent.tools.file_write_tool import FILE_WRITE_TOOL_NAME
from tabvis.agent.tools.glob_tool import GLOB_TOOL_NAME
from tabvis.agent.tools.grep_tool import GREP_TOOL_NAME
from tabvis.constants.tools import WEB_FETCH_TOOL_NAME
from tabvis.constants.tools import WEB_SEARCH_TOOL_NAME
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.shell.shell_tool_utils import SHELL_TOOL_NAMES
from tabvis.utils.slow_operations import json_stringify

if TYPE_CHECKING:  # type-only
    from tabvis.constants.query_source import QuerySource
    from tabvis.tool import ToolUseContext
    from tabvis.types.message import Message


# Defined locally rather than imported from the shared tool-result-storage module: that module
# pulls in session storage -> messages -> API errors, which loops back through this file via
# prompt-cache-break detection, so the constant is duplicated here to avoid the cycle.
TIME_BASED_MC_CLEARED_MESSAGE = "[Old tool result content cleared]"

IMAGE_MAX_TOKEN_SIZE = 2000

# Only compact these tools.
COMPACTABLE_TOOLS: set[str] = {
    FILE_READ_TOOL_NAME,
    *SHELL_TOOL_NAMES,
    GREP_TOOL_NAME,
    GLOB_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    WEB_FETCH_TOOL_NAME,
    FILE_EDIT_TOOL_NAME,
    FILE_WRITE_TOOL_NAME,
    NOTEBOOK_EDIT_TOOL_NAME,
}


def _rough_token_count_estimation(content: str) -> int:
    """Lazy proxy for the cycle-sibling token estimator."""
    from tabvis.services.token_estimation import rough_token_count_estimation

    return rough_token_count_estimation(content)


def reset_microcompact_state() -> None:
    """No-op: cached-microcompact state is not maintained in this build."""
    return None


def _calculate_tool_result_tokens(block: dict[str, Any]) -> int:
    """Helper to calculate tool result tokens."""
    content = block.get("content")
    if not content:
        return 0

    if isinstance(content, str):
        return _rough_token_count_estimation(content)

    # Array of TextBlockParam | ImageBlockParam | DocumentBlockParam.
    total = 0
    for item in content:
        itype = item.get("type")
        if itype == "text":
            total += _rough_token_count_estimation(item["text"])
        elif itype in ("image", "document"):
            # Images/documents are approximately 2000 tokens regardless of format.
            total += IMAGE_MAX_TOKEN_SIZE
    return total


def estimate_message_tokens(messages: list[Message]) -> int:
    """Estimate token count for messages by extracting text content.

    Used for rough estimation when accurate API counts are unavailable. Pads the
    estimate by 4/3 to be conservative since we're approximating.
    """
    total_tokens = 0

    for message in messages:
        if message.get("type") not in ("user", "assistant"):
            continue

        content = message.get("message", {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            btype = block.get("type")
            if btype == "text":
                total_tokens += _rough_token_count_estimation(block["text"])
            elif btype == "tool_result":
                total_tokens += _calculate_tool_result_tokens(block)
            elif btype in ("image", "document"):
                total_tokens += IMAGE_MAX_TOKEN_SIZE
            elif btype == "thinking":
                # Count only the thinking text, not the JSON wrapper or signature.
                total_tokens += _rough_token_count_estimation(block["thinking"])
            elif btype == "redacted_thinking":
                total_tokens += _rough_token_count_estimation(block["data"])
            elif btype == "tool_use":
                # Count name + input, not the JSON wrapper or id field.
                total_tokens += _rough_token_count_estimation(
                    block["name"] + json_stringify(block.get("input") or {})
                )
            else:
                # server_tool_use, web_search_tool_result, etc.
                total_tokens += _rough_token_count_estimation(json_stringify(block))

    # Pad by 4/3 to be conservative.
    import math

    return math.ceil(total_tokens * (4 / 3))


class PendingCacheEdits(TypedDict):
    trigger: str  # 'auto'
    deletedToolIds: list[str]
    baselineCacheDeletedTokens: int


class MicrocompactResult(TypedDict, total=False):
    messages: list[Message]
    compactionInfo: dict[str, Any]


def _collect_compactable_tool_ids(messages: list[Message]) -> list[str]:
    """Walk messages, collect tool_use IDs whose tool name is in COMPACTABLE_TOOLS."""
    ids: list[str] = []
    for message in messages:
        if message.get("type") == "assistant":
            content = message.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        block.get("type") == "tool_use"
                        and block.get("name") in COMPACTABLE_TOOLS
                    ):
                        ids.append(block["id"])
    return ids


def _is_main_thread_source(query_source: QuerySource | None) -> bool:
    """Prefix-match: output-style variants set ``repl_main_thread:outputStyle:<style>``."""
    return not query_source or query_source.startswith("repl_main_thread")


async def microcompact_messages(
    messages: list[Message],
    tool_use_context: ToolUseContext | None = None,
    query_source: QuerySource | None = None,
) -> MicrocompactResult:
    # Clear suppression flag at start of new microcompact attempt.
    clear_compact_warning_suppression()

    # Time-based trigger runs first and short-circuits. If the gap since the last
    # assistant message exceeds the threshold, the server cache has expired and
    # the full prefix will be rewritten regardless — so content-clear old tool
    # results now to shrink what gets rewritten.
    time_based_result = _maybe_time_based_microcompact(messages, query_source)
    if time_based_result is not None:
        return time_based_result

    # No cache-editing path in this build; autocompact handles context pressure.
    return {"messages": messages}


def evaluate_time_based_trigger(
    messages: list[Message],
    query_source: QuerySource | None,
) -> dict[str, Any] | None:
    """Check whether the time-based trigger should fire for this request.

    Returns ``{"gapMinutes": float, "config": TimeBasedMCConfig}`` when the
    trigger fires, or ``None`` when it doesn't.
    """
    config = get_time_based_mc_config()
    # Require an explicit main-thread querySource. _is_main_thread_source treats
    # None as main-thread (for cached-MC backward-compat), but several callers
    # invoke microcompact_messages without a source for analysis-only purposes.
    if not config["enabled"] or not query_source or not _is_main_thread_source(query_source):
        return None
    last_assistant = next(
        (m for m in reversed(messages) if m.get("type") == "assistant"), None
    )
    if last_assistant is None:
        return None
    gap_minutes = _gap_minutes_since(last_assistant.get("timestamp"))
    import math

    if (
        gap_minutes is None
        or not math.isfinite(gap_minutes)
        or gap_minutes < config["gapThresholdMinutes"]
    ):
        return None
    return {"gapMinutes": gap_minutes, "config": config}


def _gap_minutes_since(timestamp: Any) -> float | None:
    """Minutes between now and an ISO timestamp string (NaN-safe)."""
    if not isinstance(timestamp, str):
        return None
    from datetime import datetime

    try:
        ts = timestamp.replace("Z", "+00:00")
        then = datetime.fromisoformat(ts).timestamp() * 1000
    except (ValueError, OSError):
        return None
    return (time.time() * 1000 - then) / 60_000


def _maybe_time_based_microcompact(
    messages: list[Message],
    query_source: QuerySource | None,
) -> MicrocompactResult | None:
    trigger = evaluate_time_based_trigger(messages, query_source)
    if not trigger:
        return None
    gap_minutes: float = trigger["gapMinutes"]
    config: TimeBasedMCConfig = trigger["config"]

    compactable_ids = _collect_compactable_tool_ids(messages)

    # Floor at 1: keeping zero would leave the model with no working context.
    keep_recent = max(1, config["keepRecent"])
    keep_set = set(compactable_ids[-keep_recent:])
    clear_set = {cid for cid in compactable_ids if cid not in keep_set}

    if len(clear_set) == 0:
        return None

    tokens_saved = 0
    result: list[Message] = []
    for message in messages:
        content = message.get("message", {}).get("content")
        if message.get("type") != "user" or not isinstance(content, list):
            result.append(message)
            continue
        touched = False
        new_content = []
        for block in content:
            if (
                block.get("type") == "tool_result"
                and block.get("tool_use_id") in clear_set
                and block.get("content") != TIME_BASED_MC_CLEARED_MESSAGE
            ):
                tokens_saved += _calculate_tool_result_tokens(block)
                touched = True
                new_content.append({**block, "content": TIME_BASED_MC_CLEARED_MESSAGE})
            else:
                new_content.append(block)
        if not touched:
            result.append(message)
        else:
            result.append(
                {
                    **message,
                    "message": {**message["message"], "content": new_content},
                }
            )

    if tokens_saved == 0:
        return None

    log_for_debugging(
        f"[TIME-BASED MC] gap {round(gap_minutes)}min > "
        f"{config['gapThresholdMinutes']}min, cleared {len(clear_set)} tool results "
        f"(~{tokens_saved} tokens), kept last {len(keep_set)}"
    )

    suppress_compact_warning()
    reset_microcompact_state()

    return {"messages": result}
