"""Durable scheduled browser-agent tasks.

A scheduled task stores a user prompt plus either a one-time UTC timestamp or a recurring interval.
It does not store a mutable "agent run": every firing creates a normal immutable gateway Run. When
``resume_agent_id`` is set, the firing resolves that Agent's latest Run at execution time and
continues its existing Session; otherwise it creates a fresh Agent and Session.

The scheduler is intentionally conservative:

* intervals shorter than one minute are rejected;
* one task never overlaps its previous Run;
* a database claim prevents two poll iterations or manual clicks from firing the same task;
* automatic occurrence command IDs are deterministic, so a crash after Run creation is replay-safe;
* claims are cleared on process startup, allowing an interrupted occurrence to be retried.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tabvis.gateway.auth.principals import local_admin
from tabvis.gateway.methods.router import CommandContext, CommandRouter
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agents import AgentStore
from tabvis.gateway.runtime.run_store import RunStore
from tabvis.gateway.store import db
from tabvis.utils.debug import log_for_debugging

ONCE = "once"
INTERVAL = "interval"
SCHEDULE_TYPES = (ONCE, INTERVAL)
MIN_INTERVAL_SECONDS = 60

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise GatewayError(
            "VALIDATION_FAILED",
            message="scheduled timestamps must include a timezone",
        )
    return value.astimezone(timezone.utc)


def parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GatewayError(
            "VALIDATION_FAILED", message=f"'{field_name}' must be an ISO 8601 timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayError(
            "VALIDATION_FAILED", message=f"'{field_name}' must be an ISO 8601 timestamp"
        ) from exc
    return _as_utc(parsed)


def iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


@dataclass
class ScheduledTaskRecord:
    scheduled_task_id: str
    name: str
    prompt: str
    schedule_type: str
    enabled: bool = True
    run_at: str | None = None
    interval_seconds: int | None = None
    next_run_at: str | None = None
    resume_agent_id: str | None = None
    profile: str | None = None
    model: str | None = None
    max_turns: int | None = None
    principal_id: str = "local-admin"
    last_run_id: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    claim_token: str | None = None
    claimed_at: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTaskRecord":
        known = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


class ScheduledTaskStore:
    """Validation plus durable CRUD/claim operations for scheduled tasks."""

    def __init__(self, *, clock: Clock = utc_now) -> None:
        self._clock = clock

    def get(self, scheduled_task_id: str) -> ScheduledTaskRecord | None:
        data = db.get_scheduled_task(scheduled_task_id)
        return ScheduledTaskRecord.from_dict(data) if data else None

    def list(self) -> list[ScheduledTaskRecord]:
        return [ScheduledTaskRecord.from_dict(item) for item in db.list_scheduled_tasks()]

    def create(self, payload: dict[str, Any], *, principal_id: str) -> ScheduledTaskRecord:
        now = self._clock()
        values = self._validated_values(payload, now=now, partial=False)
        record = ScheduledTaskRecord(
            scheduled_task_id=ids.new_scheduled_task_id(),
            principal_id=principal_id,
            created_at=iso(now),
            updated_at=iso(now),
            **values,
        )
        with db.transaction() as conn:
            db.insert_scheduled_task(conn, record.to_dict())
        return record

    def update(
        self, scheduled_task_id: str, payload: dict[str, Any]
    ) -> ScheduledTaskRecord:
        current = self.get(scheduled_task_id)
        if current is None:
            raise GatewayError(
                "NOT_FOUND",
                message="unknown scheduled task",
                details={"scheduled_task_id": scheduled_task_id},
            )
        if current.claim_token:
            raise GatewayError(
                "CONFLICT",
                message="the scheduled task is being dispatched; retry after it starts",
                details={"scheduled_task_id": scheduled_task_id},
            )
        now = self._clock()
        merged = current.to_dict()
        merged.update(payload)
        # A scheduling-field edit establishes a new anchor/next occurrence. A pause/resume-only
        # PATCH preserves the current next occurrence instead.
        if {"schedule_type", "run_at", "interval_seconds"} & payload.keys():
            merged["next_run_at"] = None
            # An interval edit that does not carry a new start must re-anchor from *now*. The
            # stored ``run_at`` is the original creation anchor and is normally far in the past
            # for a long-lived schedule, so reusing it would recompute a due-in-the-past next
            # occurrence and fire the task immediately on the very next poll.
            if "run_at" not in payload and merged.get("schedule_type") == INTERVAL:
                merged["run_at"] = None
        values = self._validated_values(merged, now=now, partial=False)
        for key, value in values.items():
            setattr(current, key, value)
        current.updated_at = iso(now)
        with db.transaction() as conn:
            db.update_scheduled_task_in(conn, current.to_dict())
        return current

    def delete(self, scheduled_task_id: str) -> None:
        current = self.get(scheduled_task_id)
        if current is None:
            raise GatewayError(
                "NOT_FOUND",
                message="unknown scheduled task",
                details={"scheduled_task_id": scheduled_task_id},
            )
        if current.claim_token:
            raise GatewayError(
                "CONFLICT",
                message="the scheduled task is being dispatched and cannot be deleted",
                details={"scheduled_task_id": scheduled_task_id},
            )
        db.delete_scheduled_task(scheduled_task_id)

    def claim_due(self, *, limit: int = 16) -> list[tuple[ScheduledTaskRecord, str]]:
        now = self._clock()
        claimed: list[tuple[ScheduledTaskRecord, str]] = []
        for item in db.list_due_scheduled_tasks(iso(now), limit=limit):
            record = self._claim(item["scheduled_task_id"], now=now, require_due=True)
            if record is not None:
                claimed.append(record)
        return claimed

    def claim_manual(self, scheduled_task_id: str) -> tuple[ScheduledTaskRecord, str]:
        record = self._claim(scheduled_task_id, now=self._clock(), require_due=False)
        if record is None:
            current = self.get(scheduled_task_id)
            if current is None:
                raise GatewayError(
                    "NOT_FOUND",
                    message="unknown scheduled task",
                    details={"scheduled_task_id": scheduled_task_id},
                )
            raise GatewayError(
                "CONFLICT",
                message="the scheduled task is already being dispatched",
                details={"scheduled_task_id": scheduled_task_id},
            )
        return record

    def _claim(
        self, scheduled_task_id: str, *, now: datetime, require_due: bool
    ) -> tuple[ScheduledTaskRecord, str] | None:
        token = ids.new_command_id()
        with db.transaction() as conn:
            data = db.get_scheduled_task_in(conn, scheduled_task_id)
            if data is None:
                return None
            record = ScheduledTaskRecord.from_dict(data)
            if record.claim_token:
                return None
            if require_due:
                if not record.enabled or not record.next_run_at:
                    return None
                if parse_timestamp(record.next_run_at, "next_run_at") > now:
                    return None
            record.claim_token = token
            record.claimed_at = iso(now)
            record.updated_at = iso(now)
            db.update_scheduled_task_in(conn, record.to_dict())
        return record, token

    def finish_claim(
        self,
        scheduled_task_id: str,
        token: str,
        *,
        automatic: bool,
        occurrence_at: str | None,
        run_id: str | None = None,
        error: str | None = None,
        retry_in_seconds: int | None = None,
    ) -> ScheduledTaskRecord:
        now = self._clock()
        with db.transaction() as conn:
            data = db.get_scheduled_task_in(conn, scheduled_task_id)
            if data is None:
                raise GatewayError("NOT_FOUND", message="unknown scheduled task")
            record = ScheduledTaskRecord.from_dict(data)
            if record.claim_token != token:
                raise GatewayError(
                    "CONFLICT", message="the scheduled task claim is no longer current"
                )
            if retry_in_seconds is not None:
                record.enabled = True
                record.next_run_at = iso(now + timedelta(seconds=retry_in_seconds))
            elif automatic:
                self._advance(record, occurrence_at=occurrence_at, now=now)
            record.last_run_at = iso(now)
            if run_id is not None:
                record.last_run_id = run_id
            record.last_status = "queued" if run_id else "failed"
            record.last_error = error
            record.claim_token = None
            record.claimed_at = None
            record.updated_at = iso(now)
            db.update_scheduled_task_in(conn, record.to_dict())
        return record

    def recover_claims(self) -> int:
        """Clear claims left by a stopped process; deterministic command IDs prevent duplicate Runs."""
        recovered = 0
        now = iso(self._clock())
        with db.transaction() as conn:
            for item in db.list_scheduled_tasks():
                record = ScheduledTaskRecord.from_dict(item)
                if not record.claim_token:
                    continue
                record.claim_token = None
                record.claimed_at = None
                record.updated_at = now
                db.update_scheduled_task_in(conn, record.to_dict())
                recovered += 1
        return recovered

    def _advance(
        self,
        record: ScheduledTaskRecord,
        *,
        occurrence_at: str | None,
        now: datetime,
    ) -> None:
        if record.schedule_type == ONCE:
            record.enabled = False
            record.next_run_at = None
            return
        interval = int(record.interval_seconds or 0)
        base = parse_timestamp(
            occurrence_at or record.next_run_at or iso(now), "next_run_at"
        )
        next_time = base + timedelta(seconds=interval)
        while next_time <= now:
            next_time += timedelta(seconds=interval)
        record.next_run_at = iso(next_time)

    def _validated_values(
        self, payload: dict[str, Any], *, now: datetime, partial: bool
    ) -> dict[str, Any]:
        del partial  # kept explicit so future PATCH-only validation does not silently diverge
        name = str(payload.get("name") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        schedule_type = str(payload.get("schedule_type") or "").strip()
        if not name:
            raise GatewayError("VALIDATION_FAILED", message="'name' is required")
        if len(name) > 120:
            raise GatewayError(
                "VALIDATION_FAILED", message="'name' must be at most 120 characters"
            )
        if not prompt:
            raise GatewayError("VALIDATION_FAILED", message="'prompt' is required")
        if schedule_type not in SCHEDULE_TYPES:
            raise GatewayError(
                "VALIDATION_FAILED",
                message=f"'schedule_type' must be one of {', '.join(SCHEDULE_TYPES)}",
            )

        enabled = bool(payload.get("enabled", True))
        run_at: str | None = None
        interval_seconds: int | None = None
        if schedule_type == ONCE:
            parsed_run_at = parse_timestamp(payload.get("run_at"), "run_at")
            run_at = iso(parsed_run_at)
            next_run_at = run_at if enabled else payload.get("next_run_at") or run_at
        else:
            raw_interval = payload.get("interval_seconds")
            if isinstance(raw_interval, bool):
                raw_interval = 0
            try:
                interval_seconds = int(raw_interval)
            except (TypeError, ValueError) as exc:
                raise GatewayError(
                    "VALIDATION_FAILED", message="'interval_seconds' must be an integer"
                ) from exc
            if interval_seconds < MIN_INTERVAL_SECONDS:
                raise GatewayError(
                    "VALIDATION_FAILED",
                    message=f"'interval_seconds' must be at least {MIN_INTERVAL_SECONDS}",
                )
            start_value = payload.get("run_at")
            parsed_run_at = (
                parse_timestamp(start_value, "run_at")
                if start_value
                else now + timedelta(seconds=interval_seconds)
            )
            run_at = iso(parsed_run_at)
            existing_next = payload.get("next_run_at")
            next_run_at = (
                iso(parse_timestamp(existing_next, "next_run_at"))
                if existing_next and payload.get("scheduled_task_id")
                else run_at
            )

        resume_agent_id = str(payload.get("resume_agent_id") or "").strip() or None
        profile = str(payload.get("profile") or "").strip() or None
        model = str(payload.get("model") or "").strip() or None
        raw_max_turns = payload.get("max_turns")
        max_turns: int | None = None
        if raw_max_turns not in (None, ""):
            try:
                max_turns = int(raw_max_turns)
            except (TypeError, ValueError) as exc:
                raise GatewayError(
                    "VALIDATION_FAILED", message="'max_turns' must be an integer"
                ) from exc
            if max_turns < 1:
                raise GatewayError(
                    "VALIDATION_FAILED", message="'max_turns' must be at least 1"
                )
        if resume_agent_id and db.get_agent(resume_agent_id) is None:
            raise GatewayError(
                "VALIDATION_FAILED",
                message="the selected resume Agent does not exist",
                details={"agent_id": resume_agent_id},
            )
        if resume_agent_id:
            profile = None

        return {
            "name": name,
            "prompt": prompt,
            "schedule_type": schedule_type,
            "enabled": enabled,
            "run_at": run_at,
            "interval_seconds": interval_seconds,
            "next_run_at": str(next_run_at) if next_run_at else None,
            "resume_agent_id": resume_agent_id,
            "profile": profile,
            "model": model,
            "max_turns": max_turns,
        }


class ScheduledTaskScheduler:
    """Poll durable due tasks and dispatch ordinary gateway Runs."""

    def __init__(
        self,
        store: ScheduledTaskStore,
        router: CommandRouter,
        run_store: RunStore,
        agents: AgentStore,
        *,
        max_runs: int = 4,
        poll_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self._router = router
        self._runs = run_store
        self._agents = agents
        self._max_runs = max_runs
        self._poll_seconds = poll_seconds
        self._task: asyncio.Task[None] | None = None
        # Created in start(), not __init__: one ASGI app can be entered by multiple TestClient/event
        # loops over its lifetime, and asyncio primitives are loop-bound after their first wait.
        self._wake: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self.store.recover_claims()
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="tabvis-scheduled-tasks")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._wake = None

    def wake(self) -> None:
        if self._wake is not None:
            self._wake.set()

    async def run_due(self) -> list[ScheduledTaskRecord]:
        completed: list[ScheduledTaskRecord] = []
        active = db.count_active_runs(tuple(sorted(runs.ACTIVE)))
        available = max(0, self._max_runs - active)
        if available == 0:
            return completed
        for record, token in self.store.claim_due(limit=available):
            completed.append(
                await self._execute(
                    record,
                    token,
                    automatic=True,
                    occurrence_at=record.next_run_at,
                )
            )
        return completed

    async def run_now(self, scheduled_task_id: str) -> ScheduledTaskRecord:
        record, token = self.store.claim_manual(scheduled_task_id)
        return await self._execute(
            record, token, automatic=False, occurrence_at=None, raise_errors=True
        )

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad occurrence never kills the scheduler
                log_for_debugging(f"[SCHEDULER] due-task poll failed: {exc}")
            wake = self._wake
            if wake is None:
                return
            wake.clear()
            try:
                await asyncio.wait_for(wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _execute(
        self,
        record: ScheduledTaskRecord,
        token: str,
        *,
        automatic: bool,
        occurrence_at: str | None,
        raise_errors: bool = False,
    ) -> ScheduledTaskRecord:
        try:
            if db.count_active_runs(tuple(sorted(runs.ACTIVE))) >= self._max_runs:
                raise GatewayError("CAPACITY_EXCEEDED")
            if record.last_run_id:
                previous = self._runs.get_run(record.last_run_id)
                if previous is not None and not previous.is_terminal:
                    raise GatewayError(
                        "CONFLICT",
                        message="this scheduled task's previous Run is still active",
                        details={"run_id": previous.run_id},
                    )

            data = self._run_payload(record)
            command_id = (
                self._occurrence_command_id(record.scheduled_task_id, occurrence_at or "")
                if automatic
                else ids.new_command_id()
            )
            result = await self._router.dispatch(
                Command(type=CommandType.RUN_CREATE, data=data, command_id=command_id),
                CommandContext(principal=local_admin(), trace_id=f"tr_{command_id[-8:]}"),
            )
            run_id = str(result.data["run"]["run_id"])
            return self.store.finish_claim(
                record.scheduled_task_id,
                token,
                automatic=automatic,
                occurrence_at=occurrence_at,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - persist the dispatch error before returning/raising
            message = exc.message if isinstance(exc, GatewayError) else str(exc)
            retry_in_seconds = (
                MIN_INTERVAL_SECONDS
                if (
                    automatic
                    and record.schedule_type == ONCE
                    and isinstance(exc, GatewayError)
                    and exc.code
                    in {"CAPACITY_EXCEEDED", "CONFLICT", "RUN_ALREADY_ACTIVE"}
                )
                else None
            )
            finished = self.store.finish_claim(
                record.scheduled_task_id,
                token,
                automatic=automatic,
                occurrence_at=occurrence_at,
                error=message or type(exc).__name__,
                retry_in_seconds=retry_in_seconds,
            )
            if raise_errors:
                raise
            return finished

    def _run_payload(self, record: ScheduledTaskRecord) -> dict[str, Any]:
        if not record.resume_agent_id:
            return {
                "agent_id": ids.new_agent_id(),
                "session_id": ids.new_session_id(),
                "prompt": record.prompt,
                "model": record.model or "",
                "max_turns": record.max_turns,
                "profile": record.profile,
                "resume": False,
                "resume_mode": "fresh",
            }

        agent = self._agents.get(record.resume_agent_id)
        if agent is None:
            raise GatewayError(
                "NOT_FOUND",
                message="the scheduled task's resume Agent no longer exists",
                details={"agent_id": record.resume_agent_id},
            )
        latest = self._runs.latest_run_for_agent(record.resume_agent_id)
        if latest is None:
            return {
                "agent_id": record.resume_agent_id,
                "session_id": ids.new_session_id(),
                "prompt": record.prompt,
                "model": record.model or agent.default_model or "",
                "max_turns": (
                    record.max_turns
                    if record.max_turns is not None
                    else agent.default_max_turns
                ),
                "profile": agent.profile,
                "resume": False,
                "resume_mode": "fresh",
            }
        return {
            "agent_id": record.resume_agent_id,
            "session_id": latest.session_id,
            "resume_from_session_id": latest.session_id,
            "prompt": record.prompt,
            "model": record.model or latest.model or agent.default_model or "",
            "max_turns": (
                record.max_turns
                if record.max_turns is not None
                else latest.max_turns
            ),
            "profile": agent.profile,
            "resume": True,
            "resume_mode": "plus",
        }

    @staticmethod
    def _occurrence_command_id(scheduled_task_id: str, occurrence_at: str) -> str:
        digest = hashlib.sha256(
            f"{scheduled_task_id}\0{occurrence_at}".encode()
        ).hexdigest()[:20]
        return f"cmd_sched_{digest}"
