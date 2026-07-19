"""Workflow runner

:func:`run_workflow` registers a ``local_workflow`` background task, evaluates the workflow script,
and drives its ``agent()`` / ``parallel()`` / ``phase()`` / ``log()`` primitives — bounding
concurrency (16) and total agents (1000), streaming per-phase progress to the task store and the
SDK, and writing a transcript to disk. :func:`run_workflow_agent` runs one sub-agent and folds its
tokens/tool-uses into the phase totals.

Behavior notes:
- Workflow-script evaluation has no stdlib JS engine (see ``workflows/script.py``); ``run_workflow``
  surfaces that as a clear error if the script can't be evaluated.
- :func:`tabvis.agent.tools.agent_runner.run_agent` returns the final report string. :func:`run_workflow_agent`
  works from that form — it records duration / a best-effort token+tool estimate from the returned
  report. Full per-message progress is not supported.

Casing: Python identifiers snake_case; the ``LocalWorkflowTaskState`` is a plain dict whose wire keys
(``totalTokens``/``toolUses``/``childAgents``/``currentPhase``/``startedAt``/``completedAt``/…) are
kept verbatim (they round-trip through the task store / SDK progress payloads).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from tabvis.agent.task import SetAppState, create_task_state_base, generate_task_id
from tabvis.tool import ToolUseContext
from tabvis.agent.tools.agent_defs import GENERAL_PURPOSE_AGENT
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.utils.abort import AbortController
from tabvis.utils.errors import get_error_message
from tabvis.utils.task.disk_output import append_task_output, init_task_output
from tabvis.utils.task.framework import register_task, update_task_state
from tabvis.utils.task.sdk_progress import emit_task_progress
from tabvis.agent.workflows.journal import (
    append_journal_entry,
    clear_journal,
    load_journal,
    spec_hash,
)
from tabvis.agent.workflows.script import evaluate_workflow_script
from tabvis.agent.workflows.types import (
    WorkflowAgentInput,
    WorkflowAgentResult,
    WorkflowMeta,
    WorkflowPhaseState,
    WorkflowRunResult,
)

LocalWorkflowTaskState = dict[str, Any]

MAX_CONCURRENT_AGENTS = 16
MAX_TOTAL_AGENTS = 1000


async def _allow_all_can_use_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Permissive fallback gate for workflow sub-agents.

    Used only when no ``can_use_tool`` is threaded in from the caller AND the full permission
    matcher (``has_permissions_to_use_tool``) is not yet implemented. Sub-agents still carry their own
    deny-rule filtering at tool-pool assembly; this just avoids crashing the runner on the missing
    matcher. The real session gate is preferred and passed through whenever available.
    """
    return {"behavior": "allow", "updatedInput": _args[1] if len(_args) > 1 else None}


def _resolve_can_use_tool(provided: CanUseToolFn | None) -> CanUseToolFn:
    """Pick the permission gate for sub-agents: the caller's gate, else the existing matcher, else
    the permissive fallback."""
    if provided is not None:
        return provided
    try:
        from tabvis.utils.permissions.permissions import (  # type: ignore[attr-defined]
            has_permissions_to_use_tool,
        )

        return has_permissions_to_use_tool
    except ImportError:
        return _allow_all_can_use_tool


def _now_ms() -> int:
    """``Date.now()`` equivalent (epoch milliseconds)."""
    return int(time.time() * 1000)


def _root_set_app_state(context: ToolUseContext) -> SetAppState:
    """Prefer the tasks-scoped setter."""
    return getattr(context, "set_app_state_for_tasks", None) or getattr(
        context, "set_app_state", None
    )


def summarize_result(value: Any) -> str:
    """Stringify the workflow's return value."""
    if isinstance(value, str):
        return value
    if value and isinstance(value, dict):
        summary = value.get("summary")
        if isinstance(summary, str):
            return summary
        return json.dumps(value, indent=2)
    return str(value if value is not None else "")


def _task_progress(task: LocalWorkflowTaskState) -> list[dict[str, Any]]:
    """Per-phase progress records (wire keys verbatim)."""
    return [
        {
            "kind": "workflow_phase",
            "name": phase.get("name"),
            "status": phase.get("status"),
            "agent_count": phase.get("agentCount"),
            "total_tokens": phase.get("totalTokens"),
            "tool_uses": phase.get("toolUses"),
            "elapsed_ms": (phase.get("completedAt") or _now_ms()) - phase["startedAt"],
        }
        for phase in task.get("phases", [])
    ]


def _emit_workflow_progress(task_id: str, set_app_state: SetAppState) -> None:
    """Push the workflow's progress to the SDK."""
    captured: dict[str, Any] = {}

    def _read(prev: Any) -> Any:
        current = prev["tasks"].get(task_id)
        if current and current.get("type") == "local_workflow":
            captured["task"] = current
        return prev

    set_app_state(_read)
    task = captured.get("task")
    if not task:
        return
    emit_task_progress(
        task_id=task_id,
        tool_use_id=task.get("toolUseId"),
        description=task.get("description") or "",
        start_time=task.get("startTime") or task.get("startedAt") or _now_ms(),
        total_tokens=task.get("totalTokens") or 0,
        tool_uses=task.get("toolUses") or 0,
        summary=task.get("currentPhase"),
        workflow_progress=_task_progress(task),
    )


def _update_workflow_task(
    task_id: str,
    set_app_state: SetAppState,
    updater: Callable[[LocalWorkflowTaskState], LocalWorkflowTaskState],
) -> None:
    """Apply ``updater`` then emit progress."""
    update_task_state(task_id, set_app_state, updater)
    _emit_workflow_progress(task_id, set_app_state)


def _complete_current_phase(task: LocalWorkflowTaskState) -> LocalWorkflowTaskState:
    """Mark the last phase completed."""
    phases = task.get("phases", [])
    if len(phases) == 0:
        return task
    phases = list(phases)
    current = phases[-1]
    if current.get("status") == "completed":
        return task
    phases[-1] = {**current, "status": "completed", "completedAt": _now_ms()}
    return {**task, "phases": phases}


def _add_phase(task: LocalWorkflowTaskState, name: str) -> LocalWorkflowTaskState:
    """Close the current phase and start a new ``running`` one."""
    completed = _complete_current_phase(task)
    phase: WorkflowPhaseState = {
        "name": name,
        "status": "running",
        "startedAt": _now_ms(),
        "agentCount": 0,
        "totalTokens": 0,
        "toolUses": 0,
    }
    return {
        **completed,
        "currentPhase": name,
        "phases": [*completed.get("phases", []), phase],
    }


def _add_agent_result(
    task: LocalWorkflowTaskState,
    result: WorkflowAgentResult,
) -> LocalWorkflowTaskState:
    """Fold an agent's totals into the phase + task."""
    phases = list(task.get("phases", []))
    if len(phases) > 0:
        current = phases[-1]
        phases[-1] = {
            **current,
            "agentCount": current.get("agentCount", 0) + 1,
            "totalTokens": current.get("totalTokens", 0) + result["totalTokens"],
            "toolUses": current.get("toolUses", 0) + result["toolUses"],
        }
    return {
        **task,
        "phases": phases,
        "childAgents": [*task.get("childAgents", []), result],
        "totalTokens": task.get("totalTokens", 0) + result["totalTokens"],
        "toolUses": task.get("toolUses", 0) + result["toolUses"],
    }


async def run_workflow_agent(
    *,
    input: WorkflowAgentInput,
    task_id: str,
    context: ToolUseContext,
    abort_controller: AbortController,
    can_use_tool: CanUseToolFn | None = None,
) -> WorkflowAgentResult:
    """Run one sub-agent and summarize its result."""
    if not input or not isinstance(input.get("prompt"), str) or not input["prompt"].strip():
        raise ValueError("workflow agent() requires a prompt string")
    started_at = _now_ms()
    name = (input.get("name") or "").strip() or input.get("agentType") or "agent"
    agent_type = (input.get("agentType") or "").strip() or "general-purpose"
    # ``AgentDefinitionsResult`` keeps its TS wire keys (``allAgents``).
    all_agents = context.options.agent_definitions["allAgents"]
    selected_agent = next(
        (a for a in all_agents if a.agent_type == agent_type),
        GENERAL_PURPOSE_AGENT,
    )
    # The current ``run_agent`` API returns only the final report string, not per-message usage.
    # Keep the counters explicit until that API exposes token/tool metadata.
    from tabvis.agent.tools.agent_runner import run_agent

    report = await run_agent(
        prompt=input["prompt"],
        agent_def=selected_agent,
        parent_context=context,
        can_use_tool=_resolve_can_use_tool(can_use_tool),
        model=input.get("model"),
    )

    return {
        "name": name,
        "result": report or "(no result)",
        "totalTokens": 0,
        "toolUses": 0,
        "durationMs": _now_ms() - started_at,
    }


async def run_workflow(
    *,
    args: Any,
    script: str,
    script_path: str,
    meta: WorkflowMeta,
    context: ToolUseContext,
    task_id: str | None = None,
    can_use_tool: CanUseToolFn | None = None,
    resume: bool = False,
) -> WorkflowRunResult:
    """Register, evaluate, and drive a workflow to completion.

    ``can_use_tool`` is the permission gate threaded down to each sub-agent; when omitted the runner
    resolves one via :func:`_resolve_can_use_tool` (the existing matcher, else a permissive fallback).

    ``resume`` (PRD G8): when ``True`` and ``task_id`` names a prior run's journal, each ``agent()``
    call whose input spec matches a journaled result replays that result instead of re-spawning the
    sub-agent — so a killed/crashed workflow continues from where it stopped. A fresh run
    (``resume=False``) clears any stale journal and records every sub-agent result for a later resume.
    """
    if task_id is None:
        task_id = generate_task_id("local_workflow")
    set_app_state = _root_set_app_state(context)
    abort_controller = AbortController()

    def _parent_abort() -> None:
        abort_controller.abort()

    # The implemented AbortSignal fires each listener once then clears them, so this matches the TS
    # ``{ once: true }`` semantics. There is no ``removeEventListener`` on the shim — the listener is
    # already gone after firing, so the TS cleanup in ``finally`` is a no-op here.
    context.abort_controller.signal.add_event_listener("abort", _parent_abort)

    description = f"Workflow: {meta['name']}"
    state: LocalWorkflowTaskState = {
        **create_task_state_base(
            task_id, "local_workflow", description, context.tool_use_id
        ),
        "status": "running",
        "workflowName": meta["name"],
        "scriptPath": script_path,
        "phases": [],
        "childAgents": [],
        "totalTokens": 0,
        "toolUses": 0,
        "abortController": abort_controller,
        "isBackgrounded": True,
    }
    register_task(state, set_app_state)
    # A resumed run reuses the prior run's task id, so its output file already exists (init uses
    # O_EXCL). Tolerate that and continue appending to the existing transcript.
    try:
        await init_task_output(task_id)
    except FileExistsError:
        pass
    append_task_output(task_id, f"Workflow: {meta['name']}\nScript: {script_path}\n\n")

    # Resume (G8): build a spec-hash → results replay cache from the prior run's journal. A fresh run
    # starts from a clean journal and records every sub-agent result as it completes.
    replay_cache: dict[str, deque[WorkflowAgentResult]] = {}
    if resume:
        for entry in load_journal(task_id):
            replay_cache.setdefault(entry["specHash"], deque()).append(entry["result"])
    else:
        clear_journal(task_id)

    active_agents = {"count": 0}
    total_agents = {"count": 0}
    waiters: list[Callable[[], None]] = []

    async def acquire_agent_slot() -> None:
        if total_agents["count"] >= MAX_TOTAL_AGENTS:
            raise RuntimeError(f"Workflow exceeded {MAX_TOTAL_AGENTS} total agents")
        total_agents["count"] += 1
        while active_agents["count"] >= MAX_CONCURRENT_AGENTS:
            fut: asyncio.Future[None] = asyncio.get_event_loop().create_future()

            def _resolve(f: asyncio.Future[None] = fut) -> None:
                if not f.done():
                    f.set_result(None)

            waiters.append(_resolve)
            await fut
        active_agents["count"] += 1

    def release_agent_slot() -> None:
        active_agents["count"] -= 1
        if waiters:
            waiters.pop(0)()

    def phase(name: str) -> None:
        # Synchronous: a phase marker only writes output + updates task state (no awaits). Keeping it
        # sync means the phase is recorded the instant the script calls ``phase(...)`` — the engine
        # wraps it in a no-op awaitable so scripts may call it with or without ``await``.
        trimmed = str(name or "").strip()
        if not trimmed:
            raise ValueError("phase() requires a non-empty name")
        append_task_output(task_id, f"\n## {trimmed}\n")
        _update_workflow_task(task_id, set_app_state, lambda task: _add_phase(task, trimmed))

    async def agent(input: WorkflowAgentInput) -> WorkflowAgentResult:
        # Resume replay: if this exact call was journaled by a prior run, fold in the cached result
        # without spawning (or counting against the agent caps) a real sub-agent.
        h = spec_hash(input) if isinstance(input, dict) else None
        if h is not None:
            queue = replay_cache.get(h)
            if queue:
                cached = queue.popleft()
                append_task_output(
                    task_id, f"\n### Agent (cached): {cached['name']}\n{cached['result']}\n"
                )
                _update_workflow_task(
                    task_id, set_app_state, lambda task: _add_agent_result(task, cached)
                )
                return cached

        await acquire_agent_slot()
        try:
            if abort_controller.signal.aborted:
                raise RuntimeError("Workflow was stopped")
            result = await run_workflow_agent(
                input=input,
                task_id=task_id,
                context=context,
                abort_controller=abort_controller,
                can_use_tool=can_use_tool,
            )
            if h is not None:
                append_journal_entry(task_id, {"specHash": h, "result": result})
            append_task_output(task_id, f"\n### Agent: {result['name']}\n{result['result']}\n")
            _update_workflow_task(
                task_id, set_app_state, lambda task: _add_agent_result(task, result)
            )
            return result
        finally:
            release_agent_slot()

    async def parallel(items: list[Callable[[], Awaitable[Any] | Any]]) -> list[Any]:
        if not isinstance(items, list):
            raise ValueError("parallel() requires an array of functions")
        for item in items:
            if not callable(item):
                raise ValueError("parallel() entries must be functions")
        results: list[Any] = [None] * len(items)
        next_index = {"i": 0}

        async def worker() -> None:
            while next_index["i"] < len(items):
                index = next_index["i"]
                next_index["i"] += 1
                value = items[index]()
                results[index] = await value if asyncio.iscoroutine(value) else value

        await asyncio.gather(
            *(worker() for _ in range(min(MAX_CONCURRENT_AGENTS, len(items))))
        )
        return results

    def log(message: Any) -> None:
        append_task_output(task_id, f"{str(message)}\n")

    started_at = _now_ms()
    try:
        evaluated = evaluate_workflow_script(script)
        workflow = evaluated["workflow"]
        result_value = await workflow(
            {
                "args": args,
                "meta": meta,
                "agent": agent,
                "parallel": parallel,
                "phase": phase,
                "log": log,
            }
        )
        result = summarize_result(result_value)
        append_task_output(task_id, f"\n## Result\n{result}\n")

        def _on_complete(task: LocalWorkflowTaskState) -> LocalWorkflowTaskState:
            return {
                **_complete_current_phase(task),
                "status": "completed",
                "result": result,
                "endTime": _now_ms(),
                "notified": True,
                "abortController": None,
            }

        _update_workflow_task(task_id, set_app_state, _on_complete)
        final_task = context.get_app_state()["tasks"].get(task_id)
        total_tokens = (
            final_task["totalTokens"]
            if final_task and final_task.get("type") == "local_workflow"
            else 0
        )
        tool_uses = (
            final_task["toolUses"]
            if final_task and final_task.get("type") == "local_workflow"
            else 0
        )
        return {
            "taskId": task_id,
            "workflowName": meta["name"],
            "scriptPath": script_path,
            "result": result,
            "totalTokens": total_tokens,
            "toolUses": tool_uses,
            "durationMs": _now_ms() - started_at,
        }
    except Exception as error:
        msg = get_error_message(error)
        append_task_output(task_id, f"\n## Error\n{msg}\n")

        def _on_error(task: LocalWorkflowTaskState) -> LocalWorkflowTaskState:
            return {
                **task,
                "status": "killed" if abort_controller.signal.aborted else "failed",
                "error": msg,
                "endTime": _now_ms(),
                "notified": True,
                "abortController": None,
            }

        _update_workflow_task(task_id, set_app_state, _on_error)
        raise
