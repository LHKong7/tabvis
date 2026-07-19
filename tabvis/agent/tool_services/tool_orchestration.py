"""Tool orchestration.

``run_tools`` partitions tool calls into concurrency-safe vs serial batches
(``partition_tool_calls``) and threads the ``ToolUseContext`` through, yielding
``MessageUpdate`` = ``{'message'?, 'newContext'}``.

Every batch currently runs **serially** (concurrency-safe batches too); a true-parallel executor
for read-only batches is a later addition. Context modifiers apply immediately (serial) rather
than being buffered per-batch.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from tabvis.agent.tool_services.tool_execution import run_tool_use
from tabvis.tool import ToolUseContext, find_tool_by_name
from tabvis.types.can_use_tool import CanUseToolFn

# MessageUpdate = {'message'?: Message, 'newContext': ToolUseContext}
MessageUpdate = dict[str, Any]


def _find_assistant_message(
    assistant_messages: list[dict[str, Any]], tool_use_id: str
) -> dict[str, Any] | None:
    for m in assistant_messages:
        content = (m.get("message") or {}).get("content") or []
        if any(c.get("type") == "tool_use" and c.get("id") == tool_use_id for c in content):
            return m
    return None


def partition_tool_calls(
    tool_use_blocks: list[dict[str, Any]], tool_use_context: ToolUseContext
) -> list[dict[str, Any]]:
    """Group consecutive concurrency-safe tools into one batch; everything else is its own batch."""
    batches: list[dict[str, Any]] = []
    for tool_use in tool_use_blocks:
        tool = find_tool_by_name(tool_use_context.options.tools, tool_use["name"])
        is_safe = False
        if tool is not None:
            try:
                validated = tool.input_schema.model_validate(tool_use["input"])
                is_safe = bool(tool.is_concurrency_safe(validated))
            except Exception:  # noqa: BLE001 - conservative: treat as not concurrency-safe
                is_safe = False
        if is_safe and batches and batches[-1]["isConcurrencySafe"]:
            batches[-1]["blocks"].append(tool_use)
        else:
            batches.append({"isConcurrencySafe": is_safe, "blocks": [tool_use]})
    return batches


async def run_tools(
    tool_use_messages: list[dict[str, Any]],
    assistant_messages: list[dict[str, Any]],
    can_use_tool: CanUseToolFn,
    tool_use_context: ToolUseContext,
) -> AsyncGenerator[MessageUpdate, None]:
    current_context = tool_use_context
    for batch in partition_tool_calls(tool_use_messages, current_context):
        for tool_use in batch["blocks"]:
            if current_context.set_in_progress_tool_use_ids is not None:
                current_context.set_in_progress_tool_use_ids(
                    lambda prev, _id=tool_use["id"]: prev | {_id}
                )
            assistant_message = _find_assistant_message(assistant_messages, tool_use["id"])
            async for update in run_tool_use(
                tool_use, assistant_message or {}, can_use_tool, current_context
            ):
                context_modifier = update.get("contextModifier")
                if context_modifier:
                    current_context = context_modifier["modifyContext"](current_context)
                yield {"message": update.get("message"), "newContext": current_context}
