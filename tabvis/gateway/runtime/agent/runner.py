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
from typing import Any, Callable

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import AGGREGATE_CONTEXT, AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.browser.binding_context import bind_binding, unbind_binding
from tabvis.gateway.runtime.browser.contracts import BrowserAcquireRequest, BrowserBinding
from tabvis.gateway.runtime.browser.runtime import BrowserRuntime, set_browser_runtime
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


def _tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Bounded, DLP-safe tool summaries for the durable diagnostic stream."""
    inner = message.get("message") or {}
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    from tabvis.dlp.gateway import get_dlp_gateway

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        payload = {
            "name": str(block.get("name") or "tool")[:120],
            "input": block.get("input") if isinstance(block.get("input"), dict) else {},
        }
        decision = get_dlp_gateway().scrub("audit", payload)
        if decision.blocked or not isinstance(decision.payload, dict):
            out.append({"name": payload["name"], "input": {"error": "dlp_blocked"}})
        else:
            safe = decision.payload
            # Inputs are diagnostic hints, not a transcript replacement. Bound them before storing.
            import json

            encoded = json.dumps(safe.get("input") or {}, ensure_ascii=False, default=str)
            out.append({
                "name": str(safe.get("name") or "tool")[:120],
                "input": safe.get("input") if len(encoded) <= 1500 else {"preview": encoded[:1500]},
            })
    return out


def _safe_preview(surface: str, value: str | None) -> str:
    """DLP-gate content before it enters the durable event log / API projection."""
    from tabvis.dlp.gateway import get_dlp_gateway

    decision = get_dlp_gateway().scrub(surface, value or "")
    if decision.blocked:
        return "[dlp_blocked]"
    return str(decision.payload)[:_PREVIEW_CHARS]


def _safe_full(value: str | None) -> str:
    """DLP-gate a Run's full result for delivery — scrubbed like the preview, but NOT truncated."""
    from tabvis.dlp.gateway import get_dlp_gateway

    decision = get_dlp_gateway().scrub("api", value or "")
    return "[dlp_blocked]" if decision.blocked else str(decision.payload)


def _wire_input(value: Any) -> dict[str, Any]:
    """Serialize a validated tool input back to its public wire keys."""
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True, exclude_none=True)
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


class AgentRunLauncher:
    def __init__(
        self,
        run_store: RunStore | None = None,
        events: EventStore | None = None,
        stream_fn: StreamFn | None = None,
        *,
        context_collector: SourceCollector | None = None,
        context_runtime: ContextRuntime | None = None,
        browser_runtime: BrowserRuntime | None = None,
    ) -> None:
        self._runs = run_store or get_run_store()
        self._events = events or get_event_store()
        self._stream_fn = stream_fn
        # When a collector is wired, the launcher assembles a Context Pack before the model call and
        # injects its situational sections into the system prompt (design §11 → model call path). Off
        # by default so the loop's own assembly is untouched unless a deployment opts in.
        self._collector = context_collector
        self._context_runtime = context_runtime
        # When a Browser Runtime is wired, the run acquires a leased binding and publishes it for the
        # loop's browser tools to drive through (design §10.4). Off by default.
        self._browser = browser_runtime
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
        result_error_code: str | None = None
        binding: BrowserBinding | None = None
        token = None
        try:
            self._runs.transition(run.run_id, runs.PREPARING, expected=runs.QUEUED)
            self._runs.transition(run.run_id, runs.RUNNING, expected=runs.PREPARING)

            # Acquire a leased browser binding for the run and publish it for the loop's tools. A
            # shared-profile conflict (BROWSER_PROFILE_BUSY) fails the run deterministically (design §10.5).
            if self._browser is not None:
                try:
                    binding = await self._acquire_binding(run, context)
                except GatewayError as e:
                    self._runs.transition(run.run_id, runs.FAILED, expected=runs.RUNNING,
                                          error_code=e.code, data={"error": e.message})
                    return
                token = bind_binding(binding.binding_id)
                # The BrowserRuntime now owns browser init AND release for this Run, so the inner
                # loop must not reserve/launch/detach a second workspace (Resume Plus item 2).
                context.extra["skip_browser_init"] = True

            try:
                await self._maybe_build_context(run, context)

                async for message in self._stream(run, context):
                    mtype = message.get("type")
                    current = self._runs.get_run(run.run_id)
                    if (
                        current is not None
                        and current.status == runs.RETRYING
                        and mtype != "model_retry"
                    ):
                        self._runs.transition(
                            run.run_id,
                            runs.RUNNING,
                            expected=runs.RETRYING,
                            event_type=EventType.RUN_RESUMED,
                            data={"reason": "model_retry_finished"},
                        )
                    if mtype == "assistant":
                        turns += 1
                        tool_summaries = _tool_uses(message)
                        tool_calls += len(tool_summaries)
                        self._events.append(
                            AGGREGATE_RUN, run.run_id, EventType.ASSISTANT_MESSAGE_COMPLETED, scope=scope,
                            data={"turn": turns, "text_preview": _safe_preview(
                                "transcript", _assistant_text(message)
                            )},
                        )
                        for summary in tool_summaries:
                            self._events.append(
                                AGGREGATE_RUN, run.run_id, EventType.TOOL_COMPLETED, scope=scope,
                                data={"turn": turns, **summary},
                            )
                        self._runs.record_progress(
                            run.run_id, turns=turns, tool_calls=tool_calls
                        )
                    elif mtype == "result":
                        result_text = message.get("result")
                        is_error = bool(message.get("is_error"))
                        result_error_code = message.get("error_code")
                    elif mtype == "model_retry":
                        attempt = int(message.get("retry_attempt") or 0)
                        maximum = int(message.get("max_retries") or 0)
                        retry_in_ms = int(message.get("retry_in_ms") or 0)
                        retry_data = {
                            "reason": message.get("reason") or "model_retry",
                            "message": (
                                f"Model stream stalled · retrying "
                                f"{attempt}/{maximum} in {retry_in_ms / 1000:.1f}s"
                            ),
                            "retry_attempt": attempt,
                            "max_retries": maximum,
                            "retry_in_ms": retry_in_ms,
                        }
                        current = self._runs.get_run(run.run_id)
                        if current is not None and current.status == runs.RUNNING:
                            self._runs.transition(
                                run.run_id,
                                runs.RETRYING,
                                expected=runs.RUNNING,
                                data=retry_data,
                            )
                        elif current is not None and current.status == runs.RETRYING:
                            # A consecutive stall with no assistant message in between: the run is
                            # already RETRYING (RETRYING->RETRYING is not a legal transition), so surface
                            # the new attempt as a fresh run.retrying event rather than dropping it — else
                            # the label freezes at "retrying 1/M" for the whole multi-attempt stall.
                            self._events.append(
                                AGGREGATE_RUN,
                                run.run_id,
                                EventType.RUN_RETRYING,
                                scope=scope,
                                data=retry_data,
                            )

                terminal = runs.FAILED if is_error else runs.COMPLETED
                current = self._runs.get_run(run.run_id)
                if current is not None and current.status == runs.RETRYING:
                    self._runs.transition(
                        run.run_id,
                        runs.RUNNING,
                        expected=runs.RETRYING,
                        event_type=EventType.RUN_RESUMED,
                        data={"reason": "model_retry_finished"},
                    )
                # Persist the FULL (DLP-scrubbed but untruncated) result before the transition so a
                # channel reply can deliver it in full; the event carries only the bounded preview
                # (§7.9). Best-effort: a store hiccup must not fail the run — the preview still ships.
                if result_text and not is_error:
                    try:
                        self._runs.record_result(run.run_id, _safe_full(result_text))
                    except Exception as e:  # noqa: BLE001
                        log_for_debugging(f"[GATEWAY] could not persist full result for {run.run_id}: {e}")
                self._runs.transition(
                    run.run_id, terminal, expected=runs.RUNNING,
                    error_code=(result_error_code or "agent_error") if is_error else None,
                    data={
                        "result_preview": _safe_preview("api", result_text),
                        "error": _safe_preview("api", result_text) if is_error else None,
                    },
                    turns=turns, tool_calls=tool_calls,
                )
            finally:
                await self._release_binding(binding, token)
        except asyncio.CancelledError:
            # Cooperative cancel: the orchestrator owns the cancelling→cancelled transitions, so we
            # leave the Run state alone and just unwind (design §7.6).
            raise
        except Exception as e:  # noqa: BLE001 - a run failure is recorded, never raised to the caller
            log_for_debugging(f"[GATEWAY] run {run.run_id} failed: {e}")
            self._fail_best_effort(run.run_id, f"{type(e).__name__}: {e}", turns, tool_calls)

    async def _acquire_binding(self, run: RunRecord, context: LaunchContext) -> BrowserBinding:
        # Register the runtime so a bound tool's execute_intent resolves this very instance (§10.4).
        set_browser_runtime(self._browser)
        return await self._browser.acquire(
            BrowserAcquireRequest(agent_id=run.agent_id, run_id=run.run_id, profile=context.profile)
        )

    async def _release_binding(self, binding: BrowserBinding | None, token) -> None:
        if binding is None:
            return
        if token is not None:
            unbind_binding(token)
        try:
            await self._browser.release(binding.binding_id)
        except Exception as e:  # noqa: BLE001 - release is best-effort; the run already terminalized
            log_for_debugging(f"[GATEWAY] browser release failed for {binding.binding_id}: {e}")

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
                from tabvis.dlp.gateway import get_dlp_gateway

                decision = get_dlp_gateway().scrub("model_request", rendered)
                if not decision.blocked:
                    context.extra["system_context"] = str(decision.payload)
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
        from tabvis.gateway.runtime.agents import get_agent_store

        durable_agent = get_agent_store().get(run.agent_id)
        principal_id = (
            durable_agent.principal_id
            if durable_agent is not None and durable_agent.principal_id
            else "principal_local"
        )

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
            # A resumed Run continues the prior transcript lineage; carry the resolved identity so the
            # transcript/RunContext agree with the durable Run (Resume Plus §4.1, item 1).
            run_id=run.run_id,
            resume_mode=(context.resume_mode or ("plus" if context.resume else "fresh")),
            principal_id=principal_id,
            # When a browser binding was acquired above, the runtime owns init/release (item 2).
            skip_browser_init=bool(context.extra.get("skip_browser_init")),
            # A conversation-only resume does not write Agent Memory (§5.1).
            write_memory=(context.resume_mode != "conversation_only"),
            # Unlike the one-shot CLI, the Web runtime has a durable interaction transport. Policy
            # asks and AskUserQuestion therefore pause the same Run until the console responds.
            can_use_tool=self._interactive_can_use_tool(run),
        ):
            yield m

    def _interactive_can_use_tool(self, run: RunRecord):
        async def decide(
            tool: Any,
            input: Any,
            tool_context: Any,
            assistant_message: dict[str, Any],  # noqa: ARG001
            tool_use_id: str,  # noqa: ARG001
            force_decision: Any | None = None,  # noqa: ARG001
        ) -> dict[str, Any]:
            from tabvis.agent.tools.ask_user_question_tool import (
                ASK_USER_QUESTION_TOOL_NAME,
            )
            from tabvis.gateway.runtime import interactions
            from tabvis.gateway.runtime.interaction_service import get_interaction_service
            from tabvis.tool import get_empty_tool_permission_context
            from tabvis.utils.permissions.permissions import get_deny_rule_for_tool

            app_state = (
                tool_context.get_app_state()
                if getattr(tool_context, "get_app_state", None)
                else None
            )
            permission_context = (
                (app_state or {}).get("toolPermissionContext")
                or get_empty_tool_permission_context()
            )
            if get_deny_rule_for_tool(permission_context, tool):
                return {
                    "behavior": "deny",
                    "message": f"{tool.name} is denied by a permission rule.",
                    "decisionReason": {"type": "rule"},
                }

            decision = await tool.check_permissions(input, tool_context)
            behavior = decision.get("behavior")
            if behavior == "passthrough":
                return {
                    "behavior": "allow",
                    "updatedInput": decision.get("updatedInput", input),
                }
            if behavior != "ask":
                return decision

            kind = (
                interactions.KIND_QUESTION
                if tool.name == ASK_USER_QUESTION_TOOL_NAME
                else interactions.KIND_APPROVAL
            )
            wire = _wire_input(decision.get("updatedInput", input))
            request_payload: dict[str, Any] = {
                "tool": tool.name,
                "message": decision.get("message") or "User input required",
            }
            if kind == interactions.KIND_QUESTION:
                request_payload["questions"] = wire.get("questions") or []
            else:
                request_payload["input"] = wire
                if decision.get("decisionReason") is not None:
                    request_payload["decisionReason"] = decision["decisionReason"]

            from tabvis.dlp.gateway import get_dlp_gateway

            scrubbed = get_dlp_gateway().scrub("api", request_payload)
            if scrubbed.blocked or not isinstance(scrubbed.payload, dict):
                return {
                    "behavior": "deny",
                    "message": "DLP blocked the interaction request.",
                    "decisionReason": {"type": "dlp"},
                }

            service = get_interaction_service()
            interaction = service.request(run.run_id, kind, scrubbed.payload)
            answer = await service.wait(interaction.interaction_id)
            if kind == interactions.KIND_QUESTION:
                wire["answers"] = answer.get("answers", answer)
                return {
                    "behavior": "allow",
                    "updatedInput": wire,
                    "userModified": True,
                }
            if bool(answer.get("allow")):
                return {
                    "behavior": "allow",
                    "updatedInput": wire,
                    "userModified": True,
                }
            return {
                "behavior": "deny",
                "message": "The user denied this action.",
                "decisionReason": {"type": "user"},
            }

        return decide

    def _fail_best_effort(self, run_id: str, error: str, turns: int, tool_calls: int) -> None:
        try:
            current = self._runs.get_run(run_id)
            if current is None or current.status not in (runs.RUNNING, runs.RETRYING):
                return
            self._runs.transition(
                run_id, runs.FAILED, expected=current.status, error_code="agent_exception",
                data={"error": error}, turns=turns, tool_calls=tool_calls,
            )
        except Exception as e:  # noqa: BLE001 - already terminal (e.g. cancelled) → nothing to do
            log_for_debugging(f"[GATEWAY] could not fail run {run_id}: {e}")
