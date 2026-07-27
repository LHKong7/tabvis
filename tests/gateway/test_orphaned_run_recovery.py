"""A crash must not permanently brick the gateway.

Nothing else ever closes a run left non-terminal by a stopped process: the process that owned it is
gone, the row keeps counting toward ``count_active_runs``, and a crash with ``max_runs`` in flight
means no run can ever start again. Scheduled tasks wedge the same way — a task refuses to fire while
its previous Run is non-terminal.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.scheduled_tasks import (
    ScheduledTaskScheduler,
    ScheduledTaskStore,
)
from tabvis.gateway.store import db

NOW = datetime(2026, 7, 27, 10, tzinfo=timezone.utc)


class _RecordingLauncher:
    async def launch(self, run, context) -> None:
        del run, context

    async def abort(self, run_id: str) -> None:
        del run_id


def _gateway(max_runs: int = 4) -> GatewayApplication:
    gateway = GatewayApplication.build(launcher=_RecordingLauncher(), max_runs=max_runs)
    gateway.startup()
    return gateway


def _active() -> int:
    return db.count_active_runs(tuple(sorted(runs.ACTIVE)))


def test_recover_frees_capacity_held_by_orphaned_runs() -> None:
    gateway = _gateway()
    for i in range(4):
        run = gateway.runs.create_run(
            agent_id=f"ag{i}", session_id=f"s{i}", command_id=f"c{i}", prompt="p"
        )
        gateway.runs.transition(run.run_id, runs.PREPARING)
        gateway.runs.transition(run.run_id, runs.RUNNING)
    assert gateway.health()["capacity"]["available"] == 0

    gateway.recover()
    assert _active() == 0
    assert gateway.health()["capacity"]["available"] == 4


def test_each_active_state_reaches_a_terminal_one_over_a_declared_edge() -> None:
    gateway = _gateway(max_runs=16)
    expected: dict[str, str] = {}
    for index, path in enumerate(
        [
            [],
            [runs.PREPARING],
            [runs.PREPARING, runs.RUNNING],
            [runs.PREPARING, runs.RUNNING, runs.WAITING_FOR_INPUT],
            [runs.PREPARING, runs.RUNNING, runs.WAITING_FOR_APPROVAL],
            [runs.PREPARING, runs.RUNNING, runs.RETRYING],
            [runs.PREPARING, runs.RUNNING, runs.CANCELLING],
        ]
    ):
        run = gateway.runs.create_run(
            agent_id=f"ag_{index}", session_id="s", command_id=f"cmd_{index}", prompt="p"
        )
        for state in path:
            gateway.runs.transition(run.run_id, state)
        current = path[-1] if path else runs.QUEUED
        expected[run.run_id] = (
            runs.INTERRUPTED if current == runs.RUNNING else runs.CANCELLED
        )

    gateway.recover()
    assert _active() == 0
    for run_id, terminal in expected.items():
        record = gateway.runs.get_run(run_id)
        assert record is not None and record.status == terminal


def test_recover_is_idempotent_and_leaves_terminal_runs_alone() -> None:
    gateway = _gateway()
    done = gateway.runs.create_run(
        agent_id="ag_done", session_id="s", command_id="c", prompt="p"
    )
    gateway.runs.transition(done.run_id, runs.PREPARING)
    gateway.runs.transition(done.run_id, runs.RUNNING)
    gateway.runs.transition(done.run_id, runs.COMPLETED)

    assert gateway.runs.retire_orphaned_runs() == []
    gateway.recover()
    record = gateway.runs.get_run(done.run_id)
    assert record is not None and record.status == runs.COMPLETED


def test_a_scheduled_task_is_not_wedged_by_a_run_orphaned_at_crash() -> None:
    gateway = _gateway()
    store = ScheduledTaskStore(clock=lambda: NOW)
    scheduler = ScheduledTaskScheduler(
        store, gateway.router, gateway.runs, gateway.agents, max_runs=4
    )
    task = store.create(
        {
            "name": "hourly",
            "prompt": "p",
            "schedule_type": "interval",
            "interval_seconds": 3600,
            "run_at": (NOW - timedelta(seconds=1)).isoformat(),
        },
        principal_id="local-admin",
    )
    fired = asyncio.run(scheduler.run_due())
    orphan = gateway.runs.get_run(fired[0].last_run_id or "")
    assert orphan is not None
    gateway.runs.transition(orphan.run_id, runs.PREPARING)
    gateway.runs.transition(orphan.run_id, runs.RUNNING)  # process dies here

    def _fire_at(moment: datetime):
        later_store = ScheduledTaskStore(clock=lambda: moment)
        later = ScheduledTaskScheduler(
            later_store, gateway.router, gateway.runs, gateway.agents, max_runs=4
        )
        return asyncio.run(later.run_due())

    blocked = _fire_at(NOW + timedelta(hours=1))
    assert blocked and blocked[0].last_status == "failed"
    assert "still active" in (blocked[0].last_error or "")

    gateway.recover()
    recovered = _fire_at(NOW + timedelta(hours=2))
    assert recovered and recovered[0].last_status == "queued"
    assert recovered[0].last_error is None
    assert store.get(task.scheduled_task_id) is not None
