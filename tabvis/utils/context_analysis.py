"""Per-category token breakdown of a transcript.

:func:`analyze_context` walks the normalized message list and buckets tokens by
tool request / tool result / human / assistant / local-command / attachment, and
tracks duplicate file reads. :func:`token_stats_to_statsig_metrics` flattens the
result into a flat metric dict for analytics.

CYCLE: part of the ``context-tokens`` cluster. ``roughTokenCountEstimation`` comes
from the cycle sibling :mod:`tabvis.services.token_estimation`, imported
function-locally so this module imports standalone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tabvis.utils.messages import normalize_messages_for_api
from tabvis.utils.slow_operations import json_stringify

Message = dict[str, Any]


def _count_tokens(content: str) -> int:
    """Lazy bridge to the cycle sibling ``roughTokenCountEstimation``."""
    from tabvis.services.token_estimation import rough_token_count_estimation

    return rough_token_count_estimation(content)


@dataclass
class TokenStats:
    """Token breakdown buckets for a transcript."""

    tool_requests: dict[str, int] = field(default_factory=dict)
    tool_results: dict[str, int] = field(default_factory=dict)
    human_messages: int = 0
    assistant_messages: int = 0
    local_command_outputs: int = 0
    other: int = 0
    attachments: dict[str, int] = field(default_factory=dict)
    duplicate_file_reads: dict[str, dict[str, int]] = field(default_factory=dict)
    total: int = 0


def analyze_context(messages: list[Message]) -> TokenStats:
    """Bucket transcript tokens by category and detect duplicate file reads."""
    stats = TokenStats()

    tool_ids_to_tool_names: dict[str, str] = {}
    read_tool_id_to_file_path: dict[str, str] = {}
    file_read_stats: dict[str, dict[str, int]] = {}

    for msg in messages:
        if msg.get("type") == "attachment":
            attachment = msg.get("attachment") or {}
            att_type = attachment.get("type") or "unknown"
            stats.attachments[att_type] = stats.attachments.get(att_type, 0) + 1

    normalized_messages = normalize_messages_for_api(messages)
    for msg in normalized_messages:
        content = msg.get("message", {}).get("content")

        # Not sure if this path is still used, but adding as a fallback.
        if isinstance(content, str):
            tokens = _count_tokens(content)
            stats.total += tokens
            if msg.get("type") == "user" and "local-command-stdout" in content:
                stats.local_command_outputs += tokens
            elif msg.get("type") == "user":
                stats.human_messages += tokens
            else:
                stats.assistant_messages += tokens
        elif content is not None:
            for block in content:
                _process_block(
                    block,
                    msg,
                    stats,
                    tool_ids_to_tool_names,
                    read_tool_id_to_file_path,
                    file_read_stats,
                )

    # Calculate duplicate file reads.
    for path, data in file_read_stats.items():
        if data["count"] > 1:
            average_tokens_per_read = data["totalTokens"] // data["count"]
            duplicate_tokens = average_tokens_per_read * (data["count"] - 1)
            stats.duplicate_file_reads[path] = {
                "count": data["count"],
                "tokens": duplicate_tokens,
            }

    return stats


def _process_block(
    block: dict[str, Any],
    message: Message,
    stats: TokenStats,
    tool_ids: dict[str, str],
    read_tool_paths: dict[str, str],
    file_reads: dict[str, dict[str, int]],
) -> None:
    tokens = _count_tokens(json_stringify(block))
    stats.total += tokens

    block_type = block.get("type") if isinstance(block, dict) else None
    msg_is_user = message.get("type") == "user"

    if block_type == "text":
        if msg_is_user and "text" in block and "local-command-stdout" in block["text"]:
            stats.local_command_outputs += tokens
        elif msg_is_user:
            stats.human_messages += tokens
        else:
            stats.assistant_messages += tokens

    elif block_type == "tool_use":
        if "name" in block and "id" in block:
            tool_name = block.get("name") or "unknown"
            _increment(stats.tool_requests, tool_name, tokens)
            tool_ids[block["id"]] = tool_name

            # Track Read tool file paths.
            block_input = block.get("input")
            if (
                tool_name == "Read"
                and isinstance(block_input, dict)
                and "file_path" in block_input
            ):
                read_tool_paths[block["id"]] = str(block_input["file_path"])

    elif block_type == "tool_result":
        if "tool_use_id" in block:
            tool_name = tool_ids.get(block["tool_use_id"], "unknown")
            _increment(stats.tool_results, tool_name, tokens)

            # Track file read tokens.
            if tool_name == "Read":
                path = read_tool_paths.get(block["tool_use_id"])
                if path:
                    current = file_reads.get(path, {"count": 0, "totalTokens": 0})
                    file_reads[path] = {
                        "count": current["count"] + 1,
                        "totalTokens": current["totalTokens"] + tokens,
                    }

    elif block_type in _OTHER_BLOCK_TYPES:
        # Don't care about these for now.
        stats.other += tokens


_OTHER_BLOCK_TYPES = frozenset(
    {
        "image",
        "server_tool_use",
        "web_search_tool_result",
        "search_result",
        "document",
        "thinking",
        "redacted_thinking",
        "code_execution_tool_result",
        "mcp_tool_use",
        "mcp_tool_result",
        "container_upload",
        "web_fetch_tool_result",
        "bash_code_execution_tool_result",
        "text_editor_code_execution_tool_result",
        "tool_search_tool_result",
        "compaction",
    }
)


def _increment(map_: dict[str, int], key: str, value: int) -> None:
    map_[key] = map_.get(key, 0) + value


def _js_round(value: float) -> int:
    """JS ``Math.round``: round half toward +Infinity (non-negative inputs)."""
    import math

    return math.floor(value + 0.5)


def token_stats_to_statsig_metrics(stats: TokenStats) -> dict[str, int]:
    """Flatten :class:`TokenStats` into a flat analytics metric dict."""
    metrics: dict[str, int] = {
        "total_tokens": stats.total,
        "human_message_tokens": stats.human_messages,
        "assistant_message_tokens": stats.assistant_messages,
        "local_command_output_tokens": stats.local_command_outputs,
        "other_tokens": stats.other,
    }

    for att_type, cnt in stats.attachments.items():
        metrics[f"attachment_{att_type}_count"] = cnt

    for tool, tokens in stats.tool_requests.items():
        metrics[f"tool_request_{tool}_tokens"] = tokens

    for tool, tokens in stats.tool_results.items():
        metrics[f"tool_result_{tool}_tokens"] = tokens

    duplicate_total = sum(d["tokens"] for d in stats.duplicate_file_reads.values())

    metrics["duplicate_read_tokens"] = duplicate_total
    metrics["duplicate_read_file_count"] = len(stats.duplicate_file_reads)

    if stats.total > 0:
        metrics["human_message_percent"] = _js_round(
            (stats.human_messages / stats.total) * 100
        )
        metrics["assistant_message_percent"] = _js_round(
            (stats.assistant_messages / stats.total) * 100
        )
        metrics["local_command_output_percent"] = _js_round(
            (stats.local_command_outputs / stats.total) * 100
        )
        metrics["duplicate_read_percent"] = _js_round(
            (duplicate_total / stats.total) * 100
        )

        tool_request_total = sum(stats.tool_requests.values())
        tool_result_total = sum(stats.tool_results.values())

        metrics["tool_request_percent"] = _js_round(
            (tool_request_total / stats.total) * 100
        )
        metrics["tool_result_percent"] = _js_round(
            (tool_result_total / stats.total) * 100
        )

        # Individual tool request percentages.
        for tool, tokens in stats.tool_requests.items():
            metrics[f"tool_request_{tool}_percent"] = _js_round(
                (tokens / stats.total) * 100
            )

        # Individual tool result percentages.
        for tool, tokens in stats.tool_results.items():
            metrics[f"tool_result_{tool}_percent"] = _js_round(
                (tokens / stats.total) * 100
            )

    return metrics
