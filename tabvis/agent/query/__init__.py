"""The agent loop.

Headless happy path (StreamingToolExecutor OFF, auto-compaction ON, no stop-hooks/recovery): stream
the model, collect ``tool_use`` blocks, run tools, append ``tool_result`` messages, loop until the
model stops calling tools (``completed``) or ``max_turns`` is hit. Before each model call the loop
runs fail-open auto-compaction (``auto_compact_if_needed``) so a long run does not overflow context.

Python async generators can't return a value to ``async for``, so the loop yields a
:class:`Terminal` dataclass as its FINAL item (consumers route messages/events and capture the
trailing Terminal).

Not supported in this build: micro/reactive compaction, stop hooks, prompt-too-long / max-output
recovery, fallback-model reset, attachments, the streaming tool executor.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from tabvis.agent.query.deps import QueryDeps, production_deps
from tabvis.agent.tool_services.tool_orchestration import run_tools
from tabvis.tool import ToolUseContext
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.utils.system_prompt_type import SystemPrompt

__all__ = ["Terminal", "production_deps", "query"]


@dataclass
class Terminal:
    """Why the loop ended."""

    reason: str
    turn_count: int | None = None
    error: Any = None


@dataclass
class QueryParams:
    messages: list[dict[str, Any]]
    system_prompt: SystemPrompt
    tools: Any
    can_use_tool: CanUseToolFn
    tool_use_context: ToolUseContext
    deps: QueryDeps = field(default_factory=lambda: production_deps)
    max_turns: int | None = None


def _tool_use_blocks(assistant_message: dict[str, Any]) -> list[dict[str, Any]]:
    content = (assistant_message.get("message") or {}).get("content") or []
    return [c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]


# Max consecutive API-error turn ends to retry before terminating.
_API_ERROR_TURN_LIMIT = 3


def _is_api_error_turn(assistant_messages: list[dict[str, Any]]) -> bool:
    """True iff the turn ended via a graceful api-error/abort sentinel (model_client converted a
    non-retryable model error into an assistant message with ``isApiErrorMessage=True``) rather than
    the model genuinely choosing to stop — such a turn must NOT consume the completion-gate budget."""
    return any(isinstance(m, dict) and m.get("isApiErrorMessage") for m in assistant_messages)


async def query(params: QueryParams) -> AsyncGenerator[Any, None]:
    """Run the agent loop, yielding stream events + messages, ending with a :class:`Terminal`."""
    messages = list(params.messages)
    tool_use_context = params.tool_use_context
    deps = params.deps
    turn_count = 0
    api_error_turns = 0
    auto_compact_tracking: dict[str, Any] = {"consecutiveFailures": 0}

    while True:
        # Auto-compaction (FAIL-OPEN). Before each model call, if the running context has grown past
        # the threshold, summarize older turns and REPLACE `messages` with the compacted set so the
        # next call_model fits the window. auto_compact_if_needed self-disables (DISABLE_*/config),
        # enforces a 3-strike circuit breaker (we persist consecutiveFailures across turns), reads the
        # model off tool_use_context.options, and swallows its own errors — the surrounding try/except
        # is belt-and-suspenders so compaction can NEVER crash the run (this must not regress the
        # graceful model-error degradation, which is a separate path inside call_model). The
        # query_source recursion guard (skips 'compact'/'session_memory') prevents compact-in-compact.
        try:
            from tabvis.agent.compact.auto_compact import auto_compact_if_needed
            from tabvis.agent.compact.compact import build_post_compact_messages

            ac_result = await auto_compact_if_needed(
                messages,
                tool_use_context,
                {
                    "systemPrompt": params.system_prompt,
                    "userContext": {},
                    "systemContext": {},
                    "toolUseContext": tool_use_context,
                    "forkContextMessages": [],
                },
                tool_use_context.options.query_source,
                auto_compact_tracking,
            )
            if "consecutiveFailures" in ac_result:
                auto_compact_tracking["consecutiveFailures"] = ac_result["consecutiveFailures"]
            if ac_result.get("wasCompacted") and ac_result.get("compactionResult"):
                messages = build_post_compact_messages(ac_result["compactionResult"])
                auto_compact_tracking["compacted"] = True
        except Exception:  # noqa: BLE001 — fail-open: compaction must never crash the headless run
            from tabvis.utils.debug import log_for_debugging

            log_for_debugging(
                "query(): auto-compaction failed; continuing with un-compacted messages"
            )

        assistant_messages: list[dict[str, Any]] = []
        tool_use_blocks: list[dict[str, Any]] = []

        async for event in deps.call_model(
            messages=messages,
            system_prompt=params.system_prompt,
            tools=params.tools,
            signal=tool_use_context.abort_controller.signal,
            tool_use_context=tool_use_context,
        ):
            yield event
            if isinstance(event, dict) and event.get("type") == "assistant":
                assistant_messages.append(event)
                tool_use_blocks.extend(_tool_use_blocks(event))

        messages.extend(assistant_messages)

        # No tool calls means the turn is complete, unless the response is a retryable API-error
        # sentinel handled below.
        if not tool_use_blocks:
            # A graceful api-error/abort turn-end (with_retry exhausted -> model_client yielded an
            # api-error assistant message, no tool calls) is not the model choosing to stop. Give
            # such turns their own small budget: drop the internal sentinel and retry the model turn;
            # once that budget is exhausted, terminate normally.
            if _is_api_error_turn(assistant_messages) and api_error_turns < _API_ERROR_TURN_LIMIT:
                api_error_turns += 1
                _drop = {id(m) for m in assistant_messages}
                messages = [m for m in messages if id(m) not in _drop]
                continue
            yield Terminal(reason="completed")
            return

        # A real tool-using turn made progress; reset the api-error budget so it bounds only
        # CONSECUTIVE api-error stalls, not cumulative ones across a long run.
        api_error_turns = 0

        tool_result_messages: list[dict[str, Any]] = []
        async for update in run_tools(
            tool_use_blocks, assistant_messages, params.can_use_tool, tool_use_context
        ):
            message = update.get("message")
            if message is not None:
                yield message
                tool_result_messages.append(message)
            if update.get("newContext") is not None:
                tool_use_context = update["newContext"]

        messages.extend(tool_result_messages)
        turn_count += 1

        if params.max_turns is not None and turn_count >= params.max_turns:
            yield Terminal(reason="max_turns", turn_count=turn_count)
            return
