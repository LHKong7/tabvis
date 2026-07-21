"""InteractionService — pause a Run on a question/approval and resume it on the answer (design §5.2).

This is the service behind the human-in-the-loop sequence (§5.2) that replaces today's headless denial
of ``AskUserQuestion``. The flow, and the invariants it upholds:

* **request** writes the Run to its waiting state and inserts the pending interaction *in one
  transaction* (§12.3), then emits ``interaction.requested``. The Run is now paused durably.
* The waiting agent task blocks on an **orchestrator-owned future keyed by ``interaction_id``**
  (§5.2), obtained via :meth:`wait` — never on an HTTP request object, so a page refresh or a dropped
  connection does not lose the pause.
* **respond** does a compare-and-set ``pending → answered`` (§5.2, §16.2: one pending interaction
  accepts at most one response), records the answer, resumes the Run, and resolves the future. A
  repeated response for the same command returns the original receipt (§5.5).
* **expire** / **cancel** move the interaction to its terminal status and terminate the Run per the
  state machine (§7.4), unblocking any waiter with the corresponding error.

Restart recovery (reconstructing pending interactions, §5.2) reads :func:`db.list_pending_interactions`;
resuming the model itself is a later phase, so this service persists everything needed for it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import AGGREGATE_INTERACTION, EventScope, EventType
from tabvis.gateway.runtime import interactions, runs
from tabvis.gateway.runtime.interactions import InteractionReceipt, InteractionRecord
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.store import db

# Interaction event names not in the §14.2 minimum catalog (that list is a minimum, not exhaustive).
_EVENT_CANCELLED = "interaction.cancelled"
_RUN_RESUMED = "run.resumed"

# Resume signals are process-global, keyed by interaction_id: the future is "orchestrator-owned"
# (design §5.2) — one owner per process — so a waiter and the resolver coordinate even when they hold
# different InteractionService handles (e.g. a page refresh answering through a fresh request path).
_SIGNALS: dict[str, "asyncio.Event"] = {}


def _signal_for(interaction_id: str) -> "asyncio.Event":
    signal = _SIGNALS.get(interaction_id)
    if signal is None:
        signal = asyncio.Event()
        _SIGNALS[interaction_id] = signal
    return signal


def reset_signals() -> None:
    """Drop all resume signals (tests)."""
    _SIGNALS.clear()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InteractionService:
    def __init__(self, run_store: RunStore | None = None, events: EventStore | None = None) -> None:
        self._runs = run_store or get_run_store()
        self._events = events or get_event_store()

    # --- request --------------------------------------------------------------------------------

    def request(
        self,
        run_id: str,
        kind: str,
        request: dict[str, Any],
        *,
        expires_at: str | None = None,
        correlation_id: str | None = None,
    ) -> InteractionRecord:
        """Pause ``run_id`` on a new question/approval and emit ``interaction.requested``.

        The Run must currently be ``running`` — it transitions to the waiting state for this ``kind``
        (§7.4). Run pause and interaction insert commit together.
        """
        if kind not in interactions.WAITING_STATE_FOR_KIND:
            raise GatewayError("VALIDATION_FAILED", message=f"Unknown interaction kind {kind!r}")
        waiting_state = interactions.WAITING_STATE_FOR_KIND[kind]

        with db.transaction() as conn:
            run = db.get_run_in(conn, run_id)
            if run is None:
                raise GatewayError("RUN_NOT_FOUND", details={"run_id": run_id})
            record = InteractionRecord(
                interaction_id=ids.new_interaction_id(),
                run_id=run_id,
                kind=kind,
                agent_id=run.get("agent_id"),
                session_id=run.get("session_id"),
                request=request,
                expires_at=expires_at,
            )
            # Pause the Run (CAS running → waiting) and append run.waiting, in this transaction.
            _, run_env = self._runs.apply_transition(
                conn, run_id, waiting_state, expected=runs.RUNNING,
                event_type=EventType.RUN_WAITING,
                data={"interaction_id": record.interaction_id, "kind": kind},
                correlation_id=correlation_id,
            )
            db.insert_interaction(conn, record.to_dict())
            int_env = self._events.append(
                AGGREGATE_INTERACTION, record.interaction_id, EventType.INTERACTION_REQUESTED,
                scope=self._scope(record), data={"kind": kind, "request": request}, conn=conn,
                correlation_id=correlation_id,
            )
        self._events.notify_live(run_env)
        self._events.notify_live(int_env)
        _signal_for(record.interaction_id)  # arm the resume signal so a waiter can attach
        return record

    # --- respond --------------------------------------------------------------------------------

    def respond(
        self,
        interaction_id: str,
        answer: dict[str, Any],
        *,
        response_command_id: str,
        correlation_id: str | None = None,
    ) -> InteractionReceipt:
        """Answer a pending interaction and resume (or fail) its Run — compare-and-set, idempotent."""
        run_env = None
        with db.transaction() as conn:
            data = db.get_interaction_in(conn, interaction_id)
            if data is None:
                raise GatewayError("INTERACTION_NOT_FOUND", details={"interaction_id": interaction_id})
            record = InteractionRecord.from_dict(data)

            if record.status == interactions.ANSWERED:
                # Idempotent replay only for the *same* response command (design §5.5); a different
                # command answering an already-answered interaction is a conflict (§16.2).
                if record.response_command_id == response_command_id:
                    return InteractionReceipt(interaction_id, record.status, record.answered_at, duplicate=True)
                raise GatewayError("INTERACTION_ALREADY_ANSWERED", details={"interaction_id": interaction_id})
            if record.status == interactions.EXPIRED:
                raise GatewayError("INTERACTION_EXPIRED", details={"interaction_id": interaction_id})
            if record.status == interactions.CANCELLED:
                raise GatewayError("INTERACTION_CANCELLED", details={"interaction_id": interaction_id})

            record.status = interactions.ANSWERED
            record.answer = answer
            record.answered_at = _utc_now()
            record.response_command_id = response_command_id
            db.update_interaction(conn, record.to_dict())
            int_env = self._events.append(
                AGGREGATE_INTERACTION, interaction_id, EventType.INTERACTION_ANSWERED,
                scope=self._scope(record), data={"kind": record.kind}, conn=conn,
                correlation_id=correlation_id,
            )
            # Resume or fail the Run depending on kind + answer (design §7.4, §13.3).
            resume_to, event_type, error_code = _resume_target(record.kind, answer)
            _, run_env = self._runs.apply_transition(
                conn, record.run_id, resume_to,
                expected=interactions.WAITING_STATE_FOR_KIND[record.kind],
                event_type=event_type, error_code=error_code,
                data={"interaction_id": interaction_id}, correlation_id=correlation_id,
            )
        self._events.notify_live(int_env)
        if run_env is not None:
            self._events.notify_live(run_env)
        self._wake(interaction_id)
        return InteractionReceipt(interaction_id, interactions.ANSWERED, record.answered_at)

    # --- expire / cancel ------------------------------------------------------------------------

    def expire(self, interaction_id: str, *, correlation_id: str | None = None) -> InteractionReceipt:
        """Expire a pending interaction and cancel its Run (design §7.4 waiting → cancelled on expiry)."""
        return self._terminate(
            interaction_id, interactions.EXPIRED, EventType.INTERACTION_EXPIRED,
            reason="interaction expired", correlation_id=correlation_id,
        )

    def cancel_for_run(self, run_id: str, *, correlation_id: str | None = None) -> InteractionReceipt | None:
        """Cancel the pending interaction blocking ``run_id`` (if any) and cancel the Run.

        Supports the Phase 2 acceptance "cancel while waiting terminates the Run and interaction"
        (§15). Returns None when the Run has no pending interaction.
        """
        with db.transaction() as conn:
            pending = db.find_pending_interaction_for_run(conn, run_id)
        if pending is None:
            return None
        return self._terminate(
            pending["interaction_id"], interactions.CANCELLED, _EVENT_CANCELLED,
            reason="cancelled by request", correlation_id=correlation_id,
        )

    def _terminate(
        self, interaction_id: str, status: str, event_type: str, *, reason: str, correlation_id: str | None,
    ) -> InteractionReceipt:
        run_env = None
        with db.transaction() as conn:
            data = db.get_interaction_in(conn, interaction_id)
            if data is None:
                raise GatewayError("INTERACTION_NOT_FOUND", details={"interaction_id": interaction_id})
            record = InteractionRecord.from_dict(data)
            if record.is_terminal:
                raise GatewayError(
                    "INTERACTION_ALREADY_ANSWERED" if record.status == interactions.ANSWERED
                    else "INTERACTION_EXPIRED" if record.status == interactions.EXPIRED
                    else "INTERACTION_CANCELLED",
                    details={"interaction_id": interaction_id},
                )
            record.status = status
            record.answered_at = _utc_now()
            db.update_interaction(conn, record.to_dict())
            int_env = self._events.append(
                AGGREGATE_INTERACTION, interaction_id, event_type,
                scope=self._scope(record), data={"reason": reason}, conn=conn, correlation_id=correlation_id,
            )
            # The waiting Run is cancelled (§7.4: waiting_for_input/approval → cancelled).
            waiting_state = interactions.WAITING_STATE_FOR_KIND[record.kind]
            run = db.get_run_in(conn, record.run_id)
            if run is not None and run.get("status") == waiting_state:
                _, run_env = self._runs.apply_transition(
                    conn, record.run_id, runs.CANCELLED, expected=waiting_state,
                    event_type=EventType.RUN_CANCELLED, error_code=reason,
                    data={"interaction_id": interaction_id}, correlation_id=correlation_id,
                )
        self._events.notify_live(int_env)
        if run_env is not None:
            self._events.notify_live(run_env)
        self._wake(interaction_id)
        return InteractionReceipt(interaction_id, status, record.answered_at)

    # --- wait (orchestrator-owned future) -------------------------------------------------------

    async def wait(self, interaction_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Block until the interaction is resolved; return the answer or raise its terminal reason.

        This is the future the agent task awaits (design §5.2). It is decoupled from any transport:
        the resolver is :meth:`respond`/:meth:`expire`/:meth:`cancel_for_run`, called from wherever the
        answer arrives.
        """
        signal = _signal_for(interaction_id)
        # If it resolved before we started waiting, the DB already reflects it — don't block.
        if not _is_pending(db.get_interaction(interaction_id)):
            return self._outcome(interaction_id)
        if timeout is not None:
            await asyncio.wait_for(signal.wait(), timeout)
        else:
            await signal.wait()
        return self._outcome(interaction_id)

    def _outcome(self, interaction_id: str) -> dict[str, Any]:
        data = db.get_interaction(interaction_id)
        if data is None:
            raise GatewayError("INTERACTION_NOT_FOUND", details={"interaction_id": interaction_id})
        record = InteractionRecord.from_dict(data)
        if record.status == interactions.ANSWERED:
            return record.answer or {}
        if record.status == interactions.EXPIRED:
            raise GatewayError("INTERACTION_EXPIRED", details={"interaction_id": interaction_id})
        if record.status == interactions.CANCELLED:
            raise GatewayError("INTERACTION_CANCELLED", details={"interaction_id": interaction_id})
        raise GatewayError("INTERACTION_NOT_FOUND", message="interaction still pending")

    def _wake(self, interaction_id: str) -> None:
        signal = _SIGNALS.get(interaction_id)
        if signal is not None:
            signal.set()

    # --- reads / helpers ------------------------------------------------------------------------

    def get(self, interaction_id: str) -> InteractionRecord | None:
        data = db.get_interaction(interaction_id)
        return InteractionRecord.from_dict(data) if data else None

    def list_pending(self) -> list[InteractionRecord]:
        return [InteractionRecord.from_dict(d) for d in db.list_pending_interactions()]

    @staticmethod
    def _scope(record: InteractionRecord) -> EventScope:
        return EventScope(agent_id=record.agent_id, session_id=record.session_id, run_id=record.run_id)


def _resume_target(kind: str, answer: dict[str, Any]) -> tuple[str, str, str | None]:
    """(run status, run event, error_code) to apply when this answer arrives.

    A question always resumes the Run. An approval resumes on allow and fails the Run on deny
    (design §7.4 waiting_for_approval → failed, §13.3).
    """
    if kind == interactions.KIND_APPROVAL and not bool(answer.get("allow")):
        return runs.FAILED, EventType.RUN_FAILED, "approval_denied"
    return runs.RUNNING, _RUN_RESUMED, None


def _is_pending(data: dict[str, Any] | None) -> bool:
    return bool(data) and data.get("status") == interactions.PENDING


_service: InteractionService | None = None


def get_interaction_service() -> InteractionService:
    global _service
    if _service is None:
        _service = InteractionService()
    return _service
