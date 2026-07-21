"""AgentRunLauncher — bridge a gateway Run to the real agent loop (design §7, §7.8).

``launch`` starts the Run's execution in its own task and returns immediately, so the creating command
gets its ``202`` while the agent runs in the background (design §9.4). The task drives the Run through
the state machine and streams the existing ``stream_agent`` loop, translating its messages into durable
domain events:

* ``preparing`` → ``running`` on start;
* one ``assistant.message.completed`` / ``tool.completed`` per model turn / tool use (bounded — no full
  DOM or secret payloads, design §7.9);
* ``completed`` or ``failed`` at the end, carrying the final turn/tool counters.

Cancel is cooperative (design §7.6): ``abort`` cancels the task; the launcher does **not** transition
the Run on cancel — the orchestrator owns the ``cancelling → cancelled`` transitions, so the two never
fight over the state.

The loop is injected (``stream_fn``) so the launcher is testable without a model or a browser; the
default is the real :func:`tabvis.ui.cli.print.stream_agent`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.events import AGGREGATE_CONTEXT, AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.context.render import render_system_context
from tabvis.gateway.runtime.context.runtime import ContextRuntime
from tabvis.gateway.runtime.context.sources import SourceCollector
from tabvis.gateway.runtime.orchestrator import LaunchContext
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.runtime.runs import RunRecord
from tabvis.utils.debug import log_for_debugging

# A stream function takes the run + context and yields the agent loop's messages.
StreamFn = Callable[..., Any]

_PREVIEW_CHARS = 2000  # bound assistant text in events — never dump full content (design §7.9)


def _count_tool_uses(message: dict[str, Any]) -> int:
    inner = message.get("message") or {}
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")


def _assistant_text(message: dict[str, Any]) -> str:
    inner = message.get("message") or {}
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    return ""


class AgentRunLauncher:
    def __init__(
        self,
        run_store: RunStore | None = None,
        events: EventStore | None = None,
        stream_fn: StreamFn | None = None,
        *,
        context_collector: SourceCollector | None = None,
        context_runtime: ContextRuntime | None = None,
    ) -> None:
        self._runs = run_store or get_run_store()
        self._events = events or get_event_store()
        self._stream_fn = stream_fn
        # When a collector is wired, the launcher assembles a Context Pack before the model call and
        # injects its situational sections into the system prompt (design §11 → model call path). Off
        # by default so the loop's own assembly is untouched unless a deployment opts in.
        self._collector = context_collector
        self._context_runtime = context_runtime
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # --- RunLauncher protocol -------------------------------------------------------------------

    async def launch(self, run: RunRecord, context: LaunchContext) -> None:
        task = asyncio.ensure_future(self._drive(run, context))
        self._tasks[run.run_id] = task
        task.add_done_callback(lambda _t, rid=run.run_id: self._tasks.pop(rid, None))

    async def abort(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()

    async def join(self, run_id: str) -> None:
        """Await the run's driving task if present (used by tests / synchronous callers)."""
        task = self._tasks.get(run_id)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    # --- driving --------------------------------------------------------------------------------

    async def _drive(self, run: RunRecord, context: LaunchContext) -> None:
        scope = EventScope(agent_id=run.agent_id, session_id=run.session_id, run_id=run.run_id)
        turns = 0
        tool_calls = 0
        result_text: str | None = None
        is_error = False
        try:
            self._runs.transition(run.run_id, runs.PREPARING, expected=runs.QUEUED)
            self._runs.transition(run.run_id, runs.RUNNING, expected=runs.PREPARING)

            await self._maybe_build_context(run, context)

            async for message in self._stream(run, context):
                mtype = message.get("type")
                if mtype == "assistant":
                    turns += 1
                    tool_calls += _count_tool_uses(message)
                    self._events.append(
                        AGGREGATE_RUN, run.run_id, EventType.ASSISTANT_MESSAGE_COMPLETED, scope=scope,
                        data={"turn": turns, "text_preview": _assistant_text(message)[:_PREVIEW_CHARS]},
                    )
                    for _ in range(_count_tool_uses(message)):
                        self._events.append(
                            AGGREGATE_RUN, run.run_id, EventType.TOOL_COMPLETED, scope=scope,
                            data={"turn": turns},
                        )
                elif mtype == "result":
                    result_text = message.get("result")
                    is_error = bool(message.get("is_error"))

            terminal = runs.FAILED if is_error else runs.COMPLETED
            self._runs.transition(
                run.run_id, terminal, expected=runs.RUNNING,
                error_code="agent_error" if is_error else None,
                data={"result_preview": (result_text or "")[:_PREVIEW_CHARS]},
                turns=turns, tool_calls=tool_calls,
            )
        except asyncio.CancelledError:
            # Cooperative cancel: the orchestrator owns the cancelling→cancelled transitions, so we
            # leave the Run state alone and just unwind (design §7.6).
            raise
        except Exception as e:  # noqa: BLE001 - a run failure is recorded, never raised to the caller
            log_for_debugging(f"[GATEWAY] run {run.run_id} failed: {e}")
            self._fail_best_effort(run.run_id, f"{type(e).__name__}: {e}", turns, tool_calls)

    async def _maybe_build_context(self, run: RunRecord, context: LaunchContext) -> None:
        """Assemble a Context Pack and stage its situational sections for the model (design §11).

        Fully guarded: context assembly is additive and must never break a run. On success the rendered
        block is stashed on the LaunchContext (consumed by :meth:`_stream`) and a durable
        ``context.pack.built`` event records the pack id, digest, and size for observability / `explain`.
        """
        if self._collector is None:
            return
        try:
            pack = await self._collector.build_pack(
                runtime=self._context_runtime, run_id=run.run_id, session_id=run.session_id,
                agent_id=run.agent_id, model=run.model or "",
            )
            rendered = render_system_context(pack)
            if rendered:
                context.extra["system_context"] = rendered
            # The pack is now authoritative for project instructions + memory too, so tell the loop to
            # suppress the base prompt's copies (full base-prompt migration under the gateway path).
            context.extra["owns_system_context"] = True
            self._events.append(
                AGGREGATE_CONTEXT, pack.context_pack_id, EventType.CONTEXT_PACK_BUILT,
                scope=EventScope(agent_id=run.agent_id, session_id=run.session_id, run_id=run.run_id),
                data={"context_pack_id": pack.context_pack_id, "digest": pack.digest,
                      "token_estimate": pack.token_estimate, "injected": bool(rendered)},
            )
        except Exception as e:  # noqa: BLE001 - context assembly never breaks a run
            log_for_debugging(f"[GATEWAY] context build failed for run {run.run_id}: {e}")

    async def _stream(self, run: RunRecord, context: LaunchContext):
        if self._stream_fn is not None:
            async for m in self._stream_fn(run, context):
                yield m
            return
        # Default: the real headless agent loop, unchanged (design non-goal: don't replace it). The
        # only new input is extra_system_context — the Context Runtime's situational block, appended to
        # the system prompt inside stream_agent.
        from tabvis.ui.cli.print import stream_agent

        async for m in stream_agent(
            context.prompt,
            model=run.model or None,
            max_turns=run.max_turns,
            include_partial_messages=context.stream_partials,
            agent_id=run.agent_id,
            profile=context.profile,
            session_id=run.session_id,
            resume=context.resume,
            teardown=False,  # the gateway owns browser teardown; keep the bundle warm past the run
            extra_system_context=context.extra.get("system_context"),
            owns_system_context=context.extra.get("owns_system_context", False),
        ):
            yield m

    def _fail_best_effort(self, run_id: str, error: str, turns: int, tool_calls: int) -> None:
        try:
            self._runs.transition(
                run_id, runs.FAILED, expected=runs.RUNNING, error_code="agent_exception",
                data={"error": error}, turns=turns, tool_calls=tool_calls,
            )
        except Exception as e:  # noqa: BLE001 - already terminal (e.g. cancelled) → nothing to do
            log_for_debugging(f"[GATEWAY] could not fail run {run_id}: {e}")
