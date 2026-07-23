"""The durable Agent aggregate (design §7.2) — identity/config that outlives its Runs.

The convergence of the legacy ``AgentRecord`` registry onto the gateway. An Agent is a durable row
(``agent_id`` + profile/cwd/principal/defaults + a lifecycle status), independent of any Run — so a
registered-but-never-run agent exists, agent-level config survives a reused agent's runs, and the
lifecycle (``active``/``disabled``/``deleted``) has a durable home. Runs stay the immutable
per-execution aggregate; the legacy ``/agents`` view becomes "this durable Agent **+** its latest Run"
(the projection in :mod:`tabvis.gateway.protocol.compatibility`).

:class:`AgentStore.ensure_in` is called inside :meth:`RunStore.create_run`'s transaction so the Agent
row and its ``agent.created`` event commit atomically with the Run — the same durability contract the
Run aggregate holds (design §12.3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from typing import Any

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import AGGREGATE_AGENT, EventEnvelope, EventScope, EventType
from tabvis.gateway.store import db

# Durable Agent lifecycle (orthogonal to a Run's execution status).
ACTIVE = "active"
DISABLED = "disabled"
DELETED = "deleted"
LIFECYCLE: tuple[str, ...] = (ACTIVE, DISABLED, DELETED)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentRecord:
    """A durable Agent (design §7.2). Identity + config, never per-execution state."""

    agent_id: str
    tenant_id: str = "local"
    name: str | None = None
    status: str = ACTIVE
    principal_id: str | None = None
    default_model: str | None = None
    default_max_turns: int | None = None
    profile: str | None = None
    cwd: str | None = None
    browser_identity_id: str | None = None
    profile_generation: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class AgentStore:
    def __init__(self, events: EventStore | None = None) -> None:
        self._events = events or get_event_store()

    # --- reads ----------------------------------------------------------------------------------

    def get(self, agent_id: str) -> AgentRecord | None:
        data = db.get_agent(agent_id)
        return AgentRecord.from_dict(data) if data else None

    def list(self, *, statuses: tuple[str, ...] | None = None) -> list[AgentRecord]:
        return [AgentRecord.from_dict(d) for d in db.list_agents(statuses)]

    # --- create-or-refresh (transaction-coupled with run creation) ------------------------------

    def ensure_in(
        self,
        conn: sqlite3.Connection,
        agent_id: str,
        *,
        model: str = "",
        max_turns: int | None = None,
        profile: str | None = None,
        cwd: str | None = None,
        principal_id: str | None = None,
        tenant_id: str = "local",
        name: str | None = None,
        scope: EventScope | None = None,
    ) -> list[EventEnvelope]:
        """Create the Agent on its first Run, else refresh its mutable durable fields.

        Runs inside the caller's open transaction so the row + ``agent.created``/``agent.updated``
        event commit atomically with the Run. Returns the event envelopes for the caller to
        ``notify_live`` after commit.
        """
        existing = db.get_agent_in(conn, agent_id)
        now = _utc_now()
        agg_scope = scope or EventScope(agent_id=agent_id)

        if existing is None:
            record = AgentRecord(
                agent_id=agent_id, tenant_id=tenant_id, name=name, status=ACTIVE,
                principal_id=principal_id, default_model=model or None, default_max_turns=max_turns,
                profile=profile, cwd=cwd, created_at=now, updated_at=now,
            )
            db.upsert_agent_in(conn, record.to_dict())
            envelope = self._events.append(
                AGGREGATE_AGENT, agent_id, EventType.AGENT_CREATED, scope=agg_scope,
                data={"status": ACTIVE, "default_model": model or None}, conn=conn,
            )
            return [envelope]

        record = AgentRecord.from_dict(existing)
        record.updated_at = now
        if model:
            record.default_model = model
        if max_turns is not None:
            record.default_max_turns = max_turns
        if profile is not None:
            record.profile = profile
        if cwd is not None:
            record.cwd = cwd
        if principal_id and not record.principal_id:
            record.principal_id = principal_id
        db.upsert_agent_in(conn, record.to_dict())
        envelope = self._events.append(
            AGGREGATE_AGENT, agent_id, EventType.AGENT_UPDATED, scope=agg_scope,
            data={"updated_at": now}, conn=conn,
        )
        return [envelope]

    def register(
        self,
        agent_id: str,
        *,
        principal_id: str | None = None,
        model: str | None = None,
        profile: str | None = None,
        cwd: str | None = None,
        tenant_id: str = "local",
        name: str | None = None,
    ) -> AgentRecord:
        """Create a durable Agent up front (before its first Run) — the registration path (§7.2).

        Lets a registered agent be listed/read as a zero-run agent. Idempotent: re-registering an
        existing agent just refreshes it.
        """
        now = _utc_now()
        with db.transaction() as conn:
            existing = db.get_agent_in(conn, agent_id)
            if existing is None:
                record = AgentRecord(
                    agent_id=agent_id, tenant_id=tenant_id, name=name, status=ACTIVE,
                    principal_id=principal_id, default_model=model or None, profile=profile, cwd=cwd,
                    created_at=now, updated_at=now,
                )
                event_type, data = EventType.AGENT_CREATED, {"status": ACTIVE, "registered": True}
            else:
                record = AgentRecord.from_dict(existing)
                record.updated_at = now
                event_type, data = EventType.AGENT_UPDATED, {"updated_at": now}
            db.upsert_agent_in(conn, record.to_dict())
            envelope = self._events.append(
                AGGREGATE_AGENT, agent_id, event_type, scope=EventScope(agent_id=agent_id), data=data, conn=conn,
            )
        self._events.notify_live(envelope)
        return record

    # --- lifecycle ------------------------------------------------------------------------------

    def set_status(self, agent_id: str, status: str) -> AgentRecord:
        """Transition the durable Agent lifecycle; emits ``agent.disabled`` / ``agent.deleted``."""
        if status not in LIFECYCLE:
            raise GatewayError("VALIDATION_FAILED", message=f"unknown agent status {status!r}")
        with db.transaction() as conn:
            existing = db.get_agent_in(conn, agent_id)
            if existing is None:
                raise GatewayError("NOT_FOUND", message="unknown agent_id", details={"agent_id": agent_id})
            record = AgentRecord.from_dict(existing)
            record.status = status
            record.updated_at = _utc_now()
            db.upsert_agent_in(conn, record.to_dict())
            event_type = {
                DELETED: EventType.AGENT_DELETED,
                DISABLED: EventType.AGENT_DISABLED,
                ACTIVE: EventType.AGENT_UPDATED,
            }[status]
            envelope = self._events.append(
                AGGREGATE_AGENT, agent_id, event_type,
                scope=EventScope(agent_id=agent_id), data={"status": status}, conn=conn,
            )
        self._events.notify_live(envelope)
        return record


_agent_store: AgentStore | None = None


def get_agent_store() -> AgentStore:
    global _agent_store
    if _agent_store is None:
        _agent_store = AgentStore()
    return _agent_store


def set_agent_store(store: AgentStore | None) -> None:
    """Install a specific store (composition root / tests)."""
    global _agent_store
    _agent_store = store
