"""Legacy `/agents` compatibility projection (design §9.8, §14 protocol/compatibility.py).

The legacy control plane is agent-centric: one `AgentRecord` conflates a durable Agent with its latest
execution. The gateway splits those into a durable Agent and immutable Runs — so the legacy views are
*projections* of gateway data: an "agent" is an ``agent_id`` and its **latest Run** (design §15 Phase 1:
"expose latest Run in compatibility views"). This module produces the legacy response shapes and maps
v1 domain events onto the legacy SSE frame names, without any second lifecycle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tabvis.gateway.protocol.events import EventEnvelope, EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.runs import RunRecord

# gateway Run status → legacy agent status (the legacy vocabulary has no waiting/preparing states).
_LEGACY_STATUS: dict[str, str] = {
    runs.QUEUED: "queued",
    runs.PREPARING: "running",
    runs.RUNNING: "running",
    runs.WAITING_FOR_INPUT: "running",
    runs.WAITING_FOR_APPROVAL: "running",
    runs.RETRYING: "running",
    runs.CANCELLING: "running",
    runs.COMPLETED: "completed",
    runs.FAILED: "failed",
    runs.INTERRUPTED: "failed",
    runs.CANCELLED: "cancelled",
}


def legacy_status(run_status: str) -> str:
    return _LEGACY_STATUS.get(run_status, run_status)


def _duration_ms(run: RunRecord) -> int | None:
    if not run.started_at:
        return None
    end = run.ended_at
    try:
        a = datetime.fromisoformat(run.started_at)
        b = datetime.fromisoformat(end) if end else a
    except (ValueError, TypeError):
        return None
    return int((b - a).total_seconds() * 1000)


def _merge_durable_agent(view: dict[str, Any], agent: dict[str, Any] | None) -> dict[str, Any]:
    """Overlay the durable Agent's fields onto a Run-derived view — the "durable Agent + latest Run"
    projection (design §7.2 convergence). Purely **additive**: the legacy execution keys (including the
    5-value ``status``) are untouched for wire-compatibility; the durable half is exposed under new keys
    (``agent_status`` is the ``active/disabled/deleted`` lifecycle, distinct from the run ``status``)."""
    if not agent:
        return view
    view["agent_status"] = agent.get("status")
    view["name"] = agent.get("name")
    view["tenant_id"] = agent.get("tenant_id")
    view["profile"] = agent.get("profile")
    view["cwd"] = agent.get("cwd")
    view["default_model"] = agent.get("default_model")
    view["default_max_turns"] = agent.get("default_max_turns")
    view["profile_generation"] = agent.get("profile_generation")
    view["agent_created_at"] = agent.get("created_at")
    view["updated_at"] = agent.get("updated_at")
    return view


def project_run_as_agent(run: RunRecord, agent: dict[str, Any] | None = None) -> dict[str, Any]:
    """A legacy agent view = the durable Agent merged over its latest Run (design §9.8, §7.2)."""
    status = legacy_status(run.status)
    view = {
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "status": status,
        "model": run.model,
        "max_turns": run.max_turns,
        "turns": run.turns,
        "tool_calls": run.tool_calls,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "duration_ms": _duration_ms(run),
        # execution fields that moved onto the Run; text lives in messages/events now.
        "result": None,
        "result_message_id": run.result_message_id,
        "error": run.error_code,
        "is_error": run.status in (runs.FAILED, runs.INTERRUPTED),
        # gateway provenance so a compat client can follow the real aggregates.
        "run_id": run.run_id,
        "conversation_id": run.conversation_id,
        "workspace_id": run.workspace_id,
        "attempt": run.attempt,
        "latest_run": run.to_dict(),
    }
    return _merge_durable_agent(view, agent)


def project_agent_only(agent: dict[str, Any]) -> dict[str, Any]:
    """A legacy agent view for a durable Agent that has **no** Run yet (design §7.2 zero-run agent).

    The execution fields are empty; ``status`` is the neutral ``queued`` (nothing has run) and the
    durable half is merged in. Lets a registered-but-never-run agent be read instead of 404'ing."""
    view = {
        "agent_id": agent.get("agent_id"),
        "session_id": None,
        "status": "queued",
        "model": agent.get("default_model"),
        "max_turns": agent.get("default_max_turns"),
        "turns": 0,
        "tool_calls": 0,
        "created_at": agent.get("created_at"),
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "result": None,
        "result_message_id": None,
        "error": None,
        "is_error": False,
        "run_id": None,
        "conversation_id": None,
        "workspace_id": None,
        "attempt": 0,
        "latest_run": None,
    }
    return _merge_durable_agent(view, agent)


def project_agent_list(
    latest_runs: list[RunRecord],
    *,
    agents_by_id: dict[str, dict[str, Any]] | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """The legacy ``GET /agents`` envelope: each agent's latest Run merged with its durable Agent."""
    by_id = agents_by_id or {}
    agents = [project_run_as_agent(r, by_id.get(r.agent_id)) for r in latest_runs]
    if status:
        agents = [a for a in agents if a["status"] == status]
    if limit:
        agents = agents[:limit]
    return {"agents": agents, "count": len(agents)}


# --- SSE frame projection (design §9.8: "project v1 domain events to legacy frames") -----------

# The legacy stream emitted named frames: agent / assistant / tool_use / result / done / cancelled /
# error. Each v1 domain event maps to zero or more of them.
def legacy_frames_for(event: EventEnvelope) -> list[dict[str, Any]]:
    et = event.type
    data = event.data or {}
    run_id = event.aggregate_id

    if et == EventType.RUN_CREATED:
        return [{"event": "agent", "data": {"agent_id": data.get("agent_id"), "run_id": run_id}}]
    if et == EventType.ASSISTANT_MESSAGE_COMPLETED:
        return [{"event": "assistant", "data": {"text": data.get("text_preview", ""), "turn": data.get("turn")}}]
    if et == EventType.TOOL_COMPLETED:
        return [{"event": "tool_use", "data": {"turn": data.get("turn")}}]
    if et == EventType.RUN_COMPLETED:
        return [
            {"event": "result", "data": {"result": data.get("result_preview", ""), "is_error": False}},
            {"event": "done", "data": {"run_id": run_id, "status": "completed"}},
        ]
    if et == EventType.RUN_FAILED:
        return [
            {"event": "error", "data": {"message": data.get("error") or data.get("result_preview", "")}},
            {"event": "done", "data": {"run_id": run_id, "status": "failed"}},
        ]
    if et == EventType.RUN_CANCELLED:
        return [{"event": "cancelled", "data": {"run_id": run_id}},
                {"event": "done", "data": {"run_id": run_id, "status": "cancelled"}}]
    return []
