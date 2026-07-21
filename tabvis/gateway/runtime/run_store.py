"""RunStore — create and advance Runs, emitting one event per transition (design §7, §12.3, §19).

This is the thin service the Run Orchestrator (a later phase) builds on. It enforces the invariants
the design pins to the Run aggregate:

* **Create and event commit together** (§12.3): a run row and its ``run.created`` event are one
  transaction, so a run never exists without its creation fact and vice versa.
* **Compare-and-set transitions** (§19 rule 7, §16.2): a transition asserts the current status matches
  the caller's expectation and that the edge is legal, then updates the row and appends the matching
  event in one transaction.
* **One active Run per Agent by default** (§7.5, §16.2): creating a second active run for an agent is
  rejected with ``RUN_ALREADY_ACTIVE`` unless explicitly allowed.

Continuing one Agent therefore yields two independent, queryable Runs — the Phase 1 acceptance test
(§15).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.protocol import ids
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.runs import RunRecord
from tabvis.gateway.store import db

# Which event a landing status emits (design §14.2). A transition may override with ``event_type``.
# The catalog is a *minimum*; the two internal-only statuses (preparing, cancelling) emit derived
# names so §19 rule 6 ("an event for every persisted transition") holds without inventing catalog
# entries the design's subscribers must know about.
_STATUS_EVENT: dict[str, str] = {
    runs.QUEUED: EventType.RUN_QUEUED,
    runs.PREPARING: "run.preparing",
    runs.RUNNING: EventType.RUN_STARTED,
    runs.WAITING_FOR_INPUT: EventType.RUN_WAITING,
    runs.WAITING_FOR_APPROVAL: EventType.RUN_WAITING,
    runs.RETRYING: EventType.RUN_RETRYING,
    runs.CANCELLING: "run.cancelling",
    runs.COMPLETED: EventType.RUN_COMPLETED,
    runs.FAILED: EventType.RUN_FAILED,
    runs.CANCELLED: EventType.RUN_CANCELLED,
    runs.INTERRUPTED: EventType.RUN_INTERRUPTED,
}

_ACTIVE_STATES: tuple[str, ...] = tuple(sorted(runs.ACTIVE))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, events: EventStore | None = None) -> None:
        self._events = events or get_event_store()

    # --- create ---------------------------------------------------------------------------------

    def create_run(
        self,
        *,
        agent_id: str,
        session_id: str,
        command_id: str,
        model: str = "",
        prompt_message_id: str = "",
        conversation_id: str | None = None,
        workspace_id: str | None = None,
        max_turns: int | None = None,
        attempt: int = 1,
        allow_concurrent: bool = False,
        correlation_id: str | None = None,
    ) -> RunRecord:
        """Create a Run and emit ``run.created`` atomically.

        Rejects a second active run for the same agent unless ``allow_concurrent`` (design §7.5).
        """
        record = RunRecord(
            run_id=ids.new_run_id(),
            agent_id=agent_id,
            session_id=session_id,
            command_id=command_id,
            model=model,
            prompt_message_id=prompt_message_id,
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            max_turns=max_turns,
            attempt=attempt,
            status=runs.QUEUED,
        )
        scope = EventScope(
            agent_id=agent_id, session_id=session_id, run_id=record.run_id,
            conversation_id=conversation_id, workspace_id=workspace_id,
        )
        with db.transaction() as conn:
            if not allow_concurrent:
                active = db.count_active_runs_for_agent(conn, agent_id, _ACTIVE_STATES)
                if active > 0:
                    raise GatewayError(
                        "RUN_ALREADY_ACTIVE",
                        message="Agent already has an active run",
                        details={"agent_id": agent_id},
                    )
            db.insert_run(conn, record.to_dict())
            envelope = self._events.append(
                AGGREGATE_RUN,
                record.run_id,
                EventType.RUN_CREATED,
                scope=scope,
                data={"agent_id": agent_id, "session_id": session_id, "model": model, "attempt": attempt},
                correlation_id=correlation_id or command_id,
                conn=conn,
            )
        self._events.notify_live(envelope)
        return record

    # --- transition -----------------------------------------------------------------------------

    def apply_transition(
        self,
        conn: Any,
        run_id: str,
        to_status: str,
        *,
        expected: str | None = None,
        event_type: str | None = None,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
        result_message_id: str | None = None,
        correlation_id: str | None = None,
        turns: int | None = None,
        tool_calls: int | None = None,
    ) -> tuple[RunRecord, Any]:
        """Compare-and-set within an already-open transaction; returns (record, undelivered event).

        This is the composable core of :meth:`transition`. A caller that must change a Run *and* do
        related work atomically — the interaction pause writes the Run to ``waiting_for_input`` and
        inserts the interaction in the same transaction — uses this and owns the commit and the live
        notify. The returned envelope has been appended durably but not yet fanned out.
        """
        current = db.get_run_in(conn, run_id)
        if current is None:
            raise GatewayError("RUN_NOT_FOUND", details={"run_id": run_id})
        record = RunRecord.from_dict(current)
        if expected is not None and record.status != expected:
            raise GatewayError(
                "CONFLICT",
                message=f"Run {run_id} is {record.status!r}, expected {expected!r}",
                details={"run_id": run_id, "status": record.status, "expected": expected},
            )
        runs.assert_transition(record.status, to_status)

        record.status = to_status
        if to_status == runs.RUNNING and record.started_at is None:
            record.started_at = _utc_now()
        if to_status in runs.TERMINAL:
            record.ended_at = _utc_now()
        if error_code is not None:
            record.error_code = error_code
        if result_message_id is not None:
            record.result_message_id = result_message_id
        if turns is not None:
            record.turns = turns
        if tool_calls is not None:
            record.tool_calls = tool_calls

        db.update_run(conn, record.to_dict())
        etype = event_type or _STATUS_EVENT.get(to_status, f"run.{to_status}")
        scope = EventScope(
            agent_id=record.agent_id, session_id=record.session_id, run_id=record.run_id,
            conversation_id=record.conversation_id, workspace_id=record.workspace_id,
        )
        envelope = self._events.append(
            AGGREGATE_RUN, run_id, etype, scope=scope,
            data=data or {}, correlation_id=correlation_id, conn=conn,
        )
        return record, envelope

    def transition(
        self,
        run_id: str,
        to_status: str,
        *,
        expected: str | None = None,
        event_type: str | None = None,
        data: dict[str, Any] | None = None,
        error_code: str | None = None,
        result_message_id: str | None = None,
        correlation_id: str | None = None,
        turns: int | None = None,
        tool_calls: int | None = None,
    ) -> RunRecord:
        """Compare-and-set a Run's status and emit the matching event, atomically.

        ``expected`` (when given) must equal the current status or ``CONFLICT`` is raised — the
        optimistic-concurrency guard from §19 rule 7. The edge itself must be legal (§7.4).
        """
        with db.transaction() as conn:
            record, envelope = self.apply_transition(
                conn, run_id, to_status, expected=expected, event_type=event_type,
                data=data, error_code=error_code, result_message_id=result_message_id,
                correlation_id=correlation_id, turns=turns, tool_calls=tool_calls,
            )
        self._events.notify_live(envelope)
        return record

    # --- reads ----------------------------------------------------------------------------------

    def get_run(self, run_id: str) -> RunRecord | None:
        data = db.get_run(run_id)
        return RunRecord.from_dict(data) if data else None

    def list_runs_for_agent(self, agent_id: str, limit: int | None = None) -> list[RunRecord]:
        return [RunRecord.from_dict(d) for d in db.list_runs_for_agent(agent_id, limit)]

    def latest_run_for_agent(self, agent_id: str) -> RunRecord | None:
        runs_ = db.list_runs_for_agent(agent_id, limit=1)
        return RunRecord.from_dict(runs_[0]) if runs_ else None

    def latest_run_per_agent(self) -> list[RunRecord]:
        """The newest run for each agent — one row per agent, newest agent first (design §9.8)."""
        seen: set[str] = set()
        out: list[RunRecord] = []
        for data in db.list_all_runs():  # newest first, so the first per agent_id is its latest
            agent_id = data.get("agent_id")
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            out.append(RunRecord.from_dict(data))
        return out


_run_store: RunStore | None = None


def get_run_store() -> RunStore:
    global _run_store
    if _run_store is None:
        _run_store = RunStore()
    return _run_store
