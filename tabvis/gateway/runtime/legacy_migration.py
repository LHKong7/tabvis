"""Migrate legacy ``AgentRecord`` envelopes into the gateway's durable Agent + Run stores (design §7,
Phase 6). Convergence step: the legacy registry conflated a durable Agent with its latest execution and
never kept per-execution history, so each legacy record migrates to **one durable Agent row + one
(latest) Run row**. Idempotent and forward-only — safe to run at every startup; already-migrated
agents/runs are skipped.

Sources (authoritative first): the on-disk ``<config-home>/agents/*.json`` envelopes, then the
``runtime.db.agents`` mirror for any id the JSON tree is missing. Each legacy record is written as a
durable Agent (identity/config/lifecycle) plus a terminal Run, in one transaction so the Run's
``run.created`` fact commits with it (the gateway durability invariant, §12.3).
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.events import AGGREGATE_AGENT, AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agents import ACTIVE, AgentRecord
from tabvis.gateway.runtime.runs import RunRecord
from tabvis.gateway.store import db
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

# legacy AgentRecord.status (5) → gateway Run status (11). A legacy queued/running record being migrated
# will never execute, so it lands terminal as `interrupted` (the honest "process is gone" state).
_STATUS_MAP: dict[str, str] = {
    "completed": runs.COMPLETED,
    "failed": runs.FAILED,
    "cancelled": runs.CANCELLED,
    "running": runs.INTERRUPTED,
    "queued": runs.INTERRUPTED,
}

_TERMINAL_EVENT: dict[str, str] = {
    runs.COMPLETED: EventType.RUN_COMPLETED,
    runs.FAILED: EventType.RUN_FAILED,
    runs.CANCELLED: EventType.RUN_CANCELLED,
    runs.INTERRUPTED: EventType.RUN_INTERRUPTED,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _legacy_records() -> Iterator[dict[str, Any]]:
    """Every legacy AgentRecord dict — JSON tree first (authoritative), then runtime.db-only ids."""
    seen: set[str] = set()
    agents_dir = os.path.join(get_tabvis_config_home_dir(), "agents")
    for path in sorted(glob.glob(os.path.join(agents_dir, "*.json"))):
        if path.endswith(".tmp"):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except Exception:  # noqa: BLE001 - a corrupt sidecar is skipped, never fatal
            continue
        agent_id = record.get("agent_id")
        if agent_id and agent_id not in seen:
            seen.add(agent_id)
            yield record
    # The SQLite mirror may hold ids whose JSON sidecar is gone (best-effort completeness).
    try:
        from tabvis.browser.persistence import db as legacy_db

        for record in legacy_db.list_agents():
            agent_id = record.get("agent_id")
            if agent_id and agent_id not in seen:
                seen.add(agent_id)
                yield record
    except Exception:  # noqa: BLE001 - the mirror is optional
        pass


def _profile_generation(agent_id: str) -> int | None:
    try:
        from tabvis.browser.profile_generation import info

        return int(info(agent_id).generation)
    except Exception:  # noqa: BLE001 - profile generations are best-effort durable metadata
        return None


def _agent_from_legacy(record: dict[str, Any], now: str) -> AgentRecord:
    return AgentRecord(
        agent_id=record["agent_id"],
        tenant_id="local",
        status=ACTIVE,  # the durable lifecycle is orthogonal to the run's execution status
        principal_id=record.get("principal_id"),
        default_model=record.get("model"),
        default_max_turns=record.get("max_turns"),
        profile=record.get("profile"),
        cwd=record.get("cwd") or None,
        profile_generation=_profile_generation(record["agent_id"]),
        created_at=record.get("created_at") or now,
        updated_at=now,
    )


def _run_from_legacy(record: dict[str, Any]) -> RunRecord:
    status = _STATUS_MAP.get(str(record.get("status") or ""), runs.INTERRUPTED)
    return RunRecord(
        run_id=record.get("run_id") or ids.new_run_id(),
        agent_id=record["agent_id"],
        session_id=record.get("session_id") or "",
        command_id="cmd_migrated_" + (record.get("run_id") or record["agent_id"]),
        model=record.get("model") or "",
        max_turns=record.get("max_turns"),
        turns=int(record.get("turns") or 0),
        tool_calls=int(record.get("tool_calls") or 0),
        status=status,
        error_code=record.get("error"),
        created_at=record.get("created_at") or _utc_now(),
        started_at=record.get("started_at"),
        ended_at=record.get("ended_at"),
    )


def migrate_legacy_agents(events: EventStore | None = None) -> dict[str, Any]:
    """Migrate every legacy AgentRecord into a durable Agent + latest Run. Idempotent."""
    event_store = events or get_event_store()
    migrated: list[str] = []
    skipped = 0
    for record in _legacy_records():
        agent_id = record.get("agent_id")
        if not agent_id:
            continue
        run = _run_from_legacy(record)
        # Skip if this agent or this run already exists in the gateway (idempotent re-run).
        if db.get_agent(agent_id) is not None or db.get_run(run.run_id) is not None:
            skipped += 1
            continue
        now = _utc_now()
        agent = _agent_from_legacy(record, now)
        scope = EventScope(agent_id=agent_id, session_id=run.session_id, run_id=run.run_id)
        try:
            envelopes = []
            with db.transaction() as conn:
                db.upsert_agent_in(conn, agent.to_dict())
                envelopes.append(event_store.append(
                    AGGREGATE_AGENT, agent_id, EventType.AGENT_CREATED,
                    scope=EventScope(agent_id=agent_id), data={"status": ACTIVE, "migrated": True}, conn=conn,
                ))
                db.insert_run(conn, run.to_dict())
                envelopes.append(event_store.append(
                    AGGREGATE_RUN, run.run_id, EventType.RUN_CREATED, scope=scope,
                    data={"agent_id": agent_id, "session_id": run.session_id, "migrated": True}, conn=conn,
                ))
                terminal_event = _TERMINAL_EVENT.get(run.status)
                if terminal_event is not None:
                    envelopes.append(event_store.append(
                        AGGREGATE_RUN, run.run_id, terminal_event, scope=scope,
                        # carry the legacy result/error text onto the terminal event (it has no run-row column).
                        data={"result_preview": record.get("result") or "", "error": record.get("error"),
                              "migrated": True}, conn=conn,
                    ))
            for envelope in envelopes:
                event_store.notify_live(envelope)
            migrated.append(agent_id)
        except Exception as exc:  # noqa: BLE001 - one bad record must not abort the whole migration
            log_for_debugging(f"[GATEWAY] legacy migration failed for {agent_id}: {exc}")
            skipped += 1
    if migrated:
        log_for_debugging(f"[GATEWAY] migrated {len(migrated)} legacy agent(s) into the gateway")
    return {"migrated": len(migrated), "skipped": skipped, "agent_ids": migrated}
