"""EventStore — the authoritative durable append and cursor read (design §3.1, §5.3, §5.5).

``append`` is the one way a fact enters the log. It assigns the two orderings the protocol promises —
a global ``cursor`` and a per-aggregate ``seq`` — and writes the event and its outbox row in a single
transaction (design §12.3). ``read`` is the resumable replay: everything after a cursor, optionally
scoped to one aggregate, with no gap and no duplicate.

``append`` accepts an optional open connection so a lifecycle transition and its event commit
together (e.g. run insert + ``run.created``). Called without one, it opens its own transaction.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from tabvis.gateway.events.subscriptions import LiveBus, get_live_bus
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.events import EventEnvelope, EventScope
from tabvis.gateway.store import db


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    """Durable, authoritative event log with an attached live fan-out."""

    def __init__(self, live_bus: LiveBus | None = None) -> None:
        self._live = live_bus or get_live_bus()

    def append(
        self,
        aggregate_type: str,
        aggregate_id: str,
        type: str,
        *,
        scope: EventScope | None = None,
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> EventEnvelope:
        """Append one fact and return its assigned envelope.

        If ``conn`` is given the append joins that transaction (the caller commits); otherwise it runs
        in its own. The live fan-out is notified only after a self-managed commit — never before the
        durable write is safe.
        """
        scope = scope or EventScope()
        if scope.run_id is None and aggregate_type == "run":
            scope = EventScope(**{**_scope_dict(scope), "run_id": aggregate_id})

        def _do(c: sqlite3.Connection) -> EventEnvelope:
            seq = db.next_seq(c, aggregate_type, aggregate_id)
            event_id = ids.new_event_id()
            occurred_at = _utc_now()
            # First insert to obtain the AUTOINCREMENT cursor, then rewrite data with the complete
            # envelope (cursor included) so a durable read never returns a partial envelope.
            partial = EventEnvelope(
                event_id=event_id,
                cursor=0,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                seq=seq,
                type=type,
                scope=scope,
                data=data or {},
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            cursor = db.insert_event(
                c,
                event_id=event_id,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                seq=seq,
                type=type,
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
                envelope=partial.to_dict(),
                created_at=occurred_at,
            )
            final = EventEnvelope(
                event_id=event_id,
                cursor=cursor,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                seq=seq,
                type=type,
                scope=scope,
                data=data or {},
                occurred_at=occurred_at,
                correlation_id=correlation_id,
                causation_id=causation_id,
            )
            c.execute("UPDATE events SET data = ? WHERE cursor = ?", (_json(final.to_dict()), cursor))
            return final

        if conn is not None:
            # Joined transaction: the caller owns commit, so we don't notify the live bus here — the
            # orchestrator does it after its commit succeeds.
            return _do(conn)

        with db.transaction() as c:
            envelope = _do(c)
        self._live.publish(envelope)
        return envelope

    def read(
        self,
        *,
        after_cursor: int = 0,
        aggregate_id: str | None = None,
        aggregate_type: str | None = None,
        limit: int | None = None,
    ) -> list[EventEnvelope]:
        """Replay durable events after ``after_cursor`` (design §5.3)."""
        rows = db.read_events(
            after_cursor=after_cursor,
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            limit=limit,
        )
        return [_envelope_from_dict(r) for r in rows]

    def latest_cursor(self) -> int:
        return db.latest_cursor()

    def notify_live(self, envelope: EventEnvelope) -> None:
        """Publish to the live fan-out — used by a caller that owned the append transaction."""
        self._live.publish(envelope)


def _scope_dict(scope: EventScope) -> dict[str, Any]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "session_id": scope.session_id,
        "run_id": scope.run_id,
        "conversation_id": scope.conversation_id,
        "workspace_id": scope.workspace_id,
    }


def _json(obj: dict[str, Any]) -> str:
    import json

    return json.dumps(obj, default=str)


def _envelope_from_dict(payload: dict[str, Any]) -> EventEnvelope:
    from tabvis.gateway.protocol.events import parse_cursor

    agg = payload.get("aggregate", {})
    sc = payload.get("scope", {})
    return EventEnvelope(
        event_id=payload["event_id"],
        cursor=parse_cursor(payload.get("cursor")),
        aggregate_type=agg.get("type", ""),
        aggregate_id=agg.get("id", ""),
        seq=int(payload.get("seq", 0)),
        type=payload["type"],
        scope=EventScope(
            tenant_id=sc.get("tenant_id", "local"),
            agent_id=sc.get("agent_id"),
            session_id=sc.get("session_id"),
            run_id=sc.get("run_id"),
            conversation_id=sc.get("conversation_id"),
            workspace_id=sc.get("workspace_id"),
        ),
        data=payload.get("data", {}),
        occurred_at=payload.get("occurred_at", ""),
        correlation_id=payload.get("correlation_id"),
        causation_id=payload.get("causation_id"),
        protocol=payload.get("protocol", EventEnvelope.protocol),
    )


_store: EventStore | None = None


def get_event_store() -> EventStore:
    """The process-wide :class:`EventStore`."""
    global _store
    if _store is None:
        _store = EventStore()
    return _store
