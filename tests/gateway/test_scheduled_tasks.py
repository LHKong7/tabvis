"""Persistent scheduled browser-agent tasks: storage, dispatch, resume, and HTTP."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from tabvis.gateway.access.http import create_gateway_app
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.auth.principals import local_admin
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.scheduled_tasks import (
    ScheduledTaskScheduler,
    ScheduledTaskStore,
)
from tabvis.gateway.store import db


class _RecordingLauncher:
    def __init__(self) -> None:
        self.launches: list[tuple[object, object]] = []

    async def launch(self, run, context) -> None:
        self.launches.append((run, context))

    async def abort(self, run_id: str) -> None:
        del run_id


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def test_schema_v8_contains_scheduled_tasks() -> None:
    conn = db.connect()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "scheduled_tasks" in names


def test_schedule_crud_is_durable_and_rejects_fast_intervals() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    store = ScheduledTaskStore(clock=lambda: now)
    with pytest.raises(GatewayError) as exc:
        store.create(
            {
                "name": "too fast",
                "prompt": "open example.com",
                "schedule_type": "interval",
                "interval_seconds": 10,
            },
            principal_id="local-admin",
        )
    assert exc.value.code == "VALIDATION_FAILED"

    record = store.create(
        {
            "name": "Daily-ish check",
            "prompt": "open example.com and summarize it",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "run_at": _iso(now + timedelta(minutes=5)),
            "profile": "default",
        },
        principal_id="local-admin",
    )
    cold_store = ScheduledTaskStore(clock=lambda: now)
    loaded = cold_store.get(record.scheduled_task_id)
    assert loaded is not None
    assert loaded.prompt == "open example.com and summarize it"
    assert loaded.next_run_at == _iso(now + timedelta(minutes=5))

    moved = cold_store.update(
        record.scheduled_task_id,
        {"run_at": _iso(now + timedelta(minutes=15))},
    )
    assert moved.next_run_at == _iso(now + timedelta(minutes=15))
    updated = cold_store.update(record.scheduled_task_id, {"enabled": False})
    assert updated.enabled is False
    cold_store.delete(record.scheduled_task_id)
    assert cold_store.get(record.scheduled_task_id) is None


def test_interval_edit_reanchors_from_now_instead_of_the_stale_start() -> None:
    """Editing only the interval must not resurrect the original (long past) ``run_at`` anchor."""
    created_at = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    record = ScheduledTaskStore(clock=lambda: created_at).create(
        {
            "name": "Hourly check",
            "prompt": "open example.com",
            "schedule_type": "interval",
            "interval_seconds": 3600,
        },
        principal_id="local-admin",
    )
    assert record.run_at == _iso(created_at + timedelta(hours=1))

    # Three days of firings later the stored anchor is stale; only ``next_run_at`` has moved on.
    later = created_at + timedelta(days=3)
    store = ScheduledTaskStore(clock=lambda: later)
    stale = store.get(record.scheduled_task_id)
    assert stale is not None
    stale.next_run_at = _iso(later + timedelta(minutes=42))
    with db.transaction() as conn:
        db.update_scheduled_task_in(conn, stale.to_dict())

    edited = store.update(record.scheduled_task_id, {"interval_seconds": 7200})
    assert edited.next_run_at == _iso(later + timedelta(hours=2))
    assert not db.list_due_scheduled_tasks(_iso(later), limit=8)

    # An explicit start still wins over the re-anchor.
    moved = store.update(
        record.scheduled_task_id,
        {"interval_seconds": 7200, "run_at": _iso(later + timedelta(minutes=5))},
    )
    assert moved.next_run_at == _iso(later + timedelta(minutes=5))


def test_due_once_task_creates_a_fresh_run_and_disables_itself() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    launcher = _RecordingLauncher()
    gateway = GatewayApplication.build(launcher=launcher)
    store = ScheduledTaskStore(clock=lambda: now)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    record = store.create(
        {
            "name": "One shot",
            "prompt": "open example.com",
            "schedule_type": "once",
            "run_at": _iso(now - timedelta(seconds=1)),
        },
        principal_id="local-admin",
    )

    completed = asyncio.run(scheduler.run_due())
    assert len(completed) == 1
    finished = store.get(record.scheduled_task_id)
    assert finished is not None
    assert finished.enabled is False
    assert finished.next_run_at is None
    assert finished.last_run_id is not None
    run = gateway.runs.get_run(finished.last_run_id)
    assert run is not None
    assert run.prompt == "open example.com"
    assert launcher.launches[0][1].resume is False


def test_run_now_resumes_latest_agent_session() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    launcher = _RecordingLauncher()
    gateway = GatewayApplication.build(launcher=launcher)
    original = gateway.runs.create_run(
        agent_id="ag_existing",
        session_id="ses_existing",
        command_id="cmd_original",
        prompt="log in",
        profile="default",
    )
    gateway.runs.transition(original.run_id, runs.PREPARING)
    gateway.runs.transition(original.run_id, runs.RUNNING)
    gateway.runs.transition(original.run_id, runs.COMPLETED)

    store = ScheduledTaskStore(clock=lambda: now)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    record = store.create(
        {
            "name": "Continue account",
            "prompt": "check the account dashboard",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "resume_agent_id": "ag_existing",
        },
        principal_id="local-admin",
    )

    asyncio.run(scheduler.run_now(record.scheduled_task_id))
    latest = gateway.runs.latest_run_for_agent("ag_existing")
    assert latest is not None and latest.run_id != original.run_id
    assert latest.session_id == "ses_existing"
    _, context = launcher.launches[-1]
    assert context.resume is True
    assert context.resume_mode == "plus"
    assert context.profile == "default"


def test_due_interval_advances_to_the_next_future_occurrence() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    store = ScheduledTaskStore(clock=lambda: now)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    record = store.create(
        {
            "name": "Interval",
            "prompt": "check once",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "run_at": _iso(now - timedelta(hours=3, minutes=5)),
        },
        principal_id="local-admin",
    )

    asyncio.run(scheduler.run_due())
    finished = store.get(record.scheduled_task_id)
    assert finished is not None and finished.enabled is True
    next_run = datetime.fromisoformat(finished.next_run_at or "")
    assert next_run > now
    assert next_run <= now + timedelta(hours=1)


def test_recovered_claim_replays_the_same_occurrence_run() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    store = ScheduledTaskStore(clock=lambda: now)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    record = store.create(
        {
            "name": "Crash safe",
            "prompt": "do this once",
            "schedule_type": "once",
            "run_at": _iso(now - timedelta(seconds=1)),
        },
        principal_id="local-admin",
    )
    claimed, token = store.claim_due()[0]
    command_id = scheduler._occurrence_command_id(  # noqa: SLF001 - exercise crash seam
        record.scheduled_task_id, claimed.next_run_at or ""
    )

    async def dispatch_before_crash() -> str:
        result = await gateway.router.dispatch(
            Command(
                type=CommandType.RUN_CREATE,
                data=scheduler._run_payload(claimed),  # noqa: SLF001
                command_id=command_id,
            ),
            CommandContext(principal=local_admin()),
        )
        return result.data["run"]["run_id"]

    original_run_id = asyncio.run(dispatch_before_crash())
    # Simulate the process stopping after run.create but before finish_claim.
    assert store.get(record.scheduled_task_id).claim_token == token
    assert store.recover_claims() == 1
    asyncio.run(scheduler.run_due())

    finished = store.get(record.scheduled_task_id)
    assert finished is not None
    assert finished.last_run_id == original_run_id
    assert len(db.list_all_runs()) == 1


def test_due_once_task_retries_when_its_previous_run_is_active() -> None:
    now = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    store = ScheduledTaskStore(clock=lambda: now)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    record = store.create(
        {
            "name": "No overlap",
            "prompt": "one at a time",
            "schedule_type": "once",
            "run_at": _iso(now - timedelta(seconds=1)),
        },
        principal_id="local-admin",
    )
    asyncio.run(scheduler.run_now(record.scheduled_task_id))

    asyncio.run(scheduler.run_due())
    deferred = store.get(record.scheduled_task_id)
    assert deferred is not None
    assert deferred.enabled is True
    assert deferred.next_run_at == _iso(now + timedelta(seconds=60))
    assert deferred.last_error == "this scheduled task's previous Run is still active"


def test_scheduled_task_http_crud_and_manual_run() -> None:
    gateway = GatewayApplication.build()
    app = create_gateway_app(gateway)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    with TestClient(app) as client:
        created = client.post(
            "/v1/scheduled-tasks",
            json={
                "name": "UI task",
                "prompt": "visit example.com",
                "schedule_type": "once",
                "run_at": _iso(future),
            },
        )
        assert created.status_code == 201
        task = created.json()["scheduled_task"]

        listing = client.get("/v1/scheduled-tasks")
        assert listing.status_code == 200
        assert listing.json()["count"] == 1

        updated = client.patch(
            f"/v1/scheduled-tasks/{task['scheduled_task_id']}",
            json={"name": "Renamed UI task", "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["scheduled_task"]["name"] == "Renamed UI task"

        run_now = client.post(
            f"/v1/scheduled-tasks/{task['scheduled_task_id']}/run"
        )
        assert run_now.status_code == 202
        assert run_now.json()["scheduled_task"]["last_run_id"].startswith("run_")

        deleted = client.delete(
            f"/v1/scheduled-tasks/{task['scheduled_task_id']}"
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
