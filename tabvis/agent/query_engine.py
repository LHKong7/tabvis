"""Session-level query wrapper — the ``ask`` entry point.

Wraps the agent loop (:func:`tabvis.agent.query.query`) and converts its yielded items into the headless
SDKMessage stream: ``system/init`` → ``assistant``/``user`` (via ``normalize_message``) →
``result``. Usage is accumulated from ``stream_event`` parts (message_start/delta/stop).

Session persistence: the completed turn is recorded to the on-disk ``<sessionId>.jsonl`` via
:func:`_persist_session_transcript` (``record_transcript``) at the Terminal boundary.

Not supported: multi-turn session state (each call runs a single turn), incremental per-message
recording, attachments, stop hooks, structured-output, partial-message replay, cost/modelUsage.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from tabvis.constants.messages import NO_CONTENT_MESSAGE
from tabvis.agent.query import QueryParams, Terminal, query
from tabvis.agent.query.deps import QueryDeps, production_deps
from tabvis.agent.api.empty_usage import empty_usage
from tabvis.agent.api.model_client import accumulate_usage, update_usage
from tabvis.tool import ToolUseContext, ToolUseContextOptions
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.utils.abort import AbortController
from tabvis.utils.cwd import get_cwd
from tabvis.utils.messages import create_user_message
from tabvis.utils.query_helpers import build_system_init_message, normalize_message

SYNTHETIC_MESSAGES = {NO_CONTENT_MESSAGE}


def _ev(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _ev_path(obj: Any, *keys: str) -> Any:
    cur = obj
    for k in keys:
        cur = _ev(cur, k)
        if cur is None:
            return None
    return cur


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return {}


async def ask(
    *,
    prompt: str | list[dict[str, Any]],
    tools: Any,
    app_state_store: Any,
    can_use_tool: CanUseToolFn,
    session_id: str,
    model: str,
    system_prompt: list[str],
    cwd: str | None = None,
    query_source: str = "sdk",
    max_turns: int | None = None,
    deps: QueryDeps | None = None,
    mcp_clients: list[Any] | None = None,
    mcp_resources: dict[str, Any] | None = None,
    agent_definitions: dict[str, Any] | None = None,
    include_partial_messages: bool = False,
    context: ToolUseContext | None = None,
    seed_messages: list[dict[str, Any]] | None = None,
    should_query: bool = True,
    result_text: str | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Run a single headless turn, yielding the SDKMessage stream.

    ``context``/``seed_messages``/``should_query``/``result_text`` support slash-command input
    (see :func:`tabvis.ui.cli.print.run_headless`): a caller can pass a pre-built ``ToolUseContext`` and a
    pre-built conversation seed. When ``should_query`` is False the model loop is skipped entirely —
    a *local* command (e.g. ``/dynamic-workflow``) has already produced its output, so we just record
    the seed and emit a ``result`` carrying ``result_text``.
    """
    cwd = cwd or get_cwd()
    start = time.monotonic()

    if context is None:
        options = ToolUseContextOptions(
            tools=tools,
            main_loop_model=model,
            is_non_interactive_session=True,
            query_source=query_source,
            mcp_clients=mcp_clients or [],
            mcp_resources=mcp_resources or {},
            agent_definitions=agent_definitions or {"activeAgents": [], "allAgents": []},
        )
        context = ToolUseContext(
            options=options,
            abort_controller=AbortController(),
            get_app_state=app_state_store.get_state,
            set_app_state=app_state_store.set_state,
            messages=[],
            set_in_progress_tool_use_ids=lambda _f: None,
        )

    # 1. system/init
    yield build_system_init_message(session_id, cwd, tools, model)

    # 2. seed the conversation (a slash command's expansion, or the plain user prompt)
    messages = list(seed_messages) if seed_messages is not None else [
        create_user_message(content=prompt)
    ]
    mutable: list[dict[str, Any]] = list(messages)

    # Local slash command already produced its result — record it and emit, no model turn.
    if not should_query:
        await _persist_session_transcript(mutable)
        yield _build_local_command_result(result_text or "", session_id, start)
        return

    params = QueryParams(
        messages=messages,
        system_prompt=system_prompt,
        tools=tools,
        can_use_tool=can_use_tool,
        tool_use_context=context,
        deps=deps or production_deps,
        max_turns=max_turns,
    )

    total_usage = empty_usage()
    current_usage = empty_usage()
    last_stop_reason: str | None = None
    turn_count = 0

    async for item in query(params):
        if isinstance(item, Terminal):
            await _persist_session_transcript(mutable)
            yield _build_result(item, mutable, turn_count, last_stop_reason, total_usage, session_id, start)
            return

        t = item.get("type") if isinstance(item, dict) else None
        if t == "assistant":
            if item["message"].get("stop_reason") is not None:
                last_stop_reason = item["message"]["stop_reason"]
            mutable.append(item)
            for sdk in normalize_message(item):
                yield sdk
        elif t == "user":
            mutable.append(item)
            for sdk in normalize_message(item):
                yield sdk
        elif t == "stream_event":
            ev = item["event"]
            etype = _ev(ev, "type")
            if etype == "message_start":
                turn_count += 1
                current_usage = update_usage(empty_usage(), _as_dict(_ev_path(ev, "message", "usage")))
            elif etype == "message_delta":
                current_usage = update_usage(current_usage, _as_dict(_ev(ev, "usage")))
                stop_reason = _ev_path(ev, "delta", "stop_reason")
                if stop_reason is not None:
                    last_stop_reason = stop_reason
            elif etype == "message_stop":
                total_usage = accumulate_usage(total_usage, current_usage)
            if include_partial_messages:
                yield {
                    "type": "stream_event",
                    "event": ev,
                    "session_id": session_id,
                    "parent_tool_use_id": None,
                    "uuid": str(uuid.uuid4()),
                }
        # system sentinel: dropped at the SDK boundary.


async def _persist_session_transcript(messages: list[dict[str, Any]]) -> None:
    """Append the completed turn to the on-disk session transcript (``<sessionId>.jsonl``).

    This records the main session transcript (sidechain/plan records are written elsewhere).
    ``record_transcript`` dedups by ``uuid`` and chains by ``parentUuid``, so a single
    end-of-turn call over the full message list records the whole chain correctly for a fresh
    session, and is idempotent if called again.

    Persistence is gated inside ``record_transcript`` (``_should_skip_persistence``: test env /
    ``cleanupPeriodDays==0`` / ``TABVIS_SKIP_PROMPT_HISTORY`` / disabled) and is **best-effort** — it
    must never break the turn, so failures are swallowed.
    """
    try:
        from tabvis.utils.session_storage import record_transcript

        await record_transcript(messages)
    except Exception:  # noqa: BLE001 - persistence is best-effort; never fail the headless turn
        from tabvis.utils.debug import log_for_debugging

        log_for_debugging("ask(): record_transcript failed; session transcript not persisted")


def _build_local_command_result(
    result_text: str, session_id: str, start: float
) -> dict[str, Any]:
    """Result SDKMessage for a local slash command that ran without a model turn (e.g.
    ``/dynamic-workflow``). ``result`` carries the command's text output; usage is empty."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "structured_output": None,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "duration_api_ms": 0,
        "num_turns": 0,
        "stop_reason": None,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "usage": empty_usage(),
        "modelUsage": {},
        "permission_denials": [],
        "uuid": str(uuid.uuid4()),
        "model": None,
        "configured_model": None,
    }


def _build_result(
    terminal: Terminal,
    mutable: list[dict[str, Any]],
    turn_count: int,
    last_stop_reason: str | None,
    total_usage: dict[str, Any],
    session_id: str,
    start: float,
) -> dict[str, Any]:
    duration_ms = int((time.monotonic() - start) * 1000)
    base = {
        "duration_ms": duration_ms,
        "duration_api_ms": 0,
        "num_turns": terminal.turn_count or turn_count,
        "stop_reason": last_stop_reason,
        "session_id": session_id,
        "total_cost_usd": 0.0,
        "usage": total_usage,
        "modelUsage": {},
        "permission_denials": [],
        "uuid": str(uuid.uuid4()),
        "model": None,
        "configured_model": None,
    }
    if terminal.reason == "max_turns":
        return {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "errors": [f"Reached maximum number of turns ({terminal.turn_count})"],
            **base,
        }

    # success: result text = last text block of the last assistant message.
    result_msg = next(
        (m for m in reversed(mutable) if m.get("type") in ("assistant", "user")), None
    )
    text_result = ""
    is_api_error = False
    if result_msg is not None and result_msg.get("type") == "assistant":
        content = result_msg["message"].get("content") or []
        last_block = content[-1] if content else None
        if (
            last_block
            and last_block.get("type") == "text"
            and last_block.get("text") not in SYNTHETIC_MESSAGES
        ):
            text_result = last_block.get("text", "")
        is_api_error = bool(result_msg.get("isApiErrorMessage"))

    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_api_error,
        "result": text_result,
        "structured_output": None,
        **base,
    }
