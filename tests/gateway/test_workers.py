"""Phase 8 — optional process separation: worker registration, leases, placement, recovery (§15)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.run_store import RunStore
from tabvis.gateway.runtime.workers import (
    KIND_AGENT,
    KIND_BROWSER,
    WorkerCoordinator,
    WorkerRegistry,
    recover_lost_runs,
)


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _coordinator(clock=None, ttl=30.0) -> WorkerCoordinator:
    return WorkerCoordinator(registry=WorkerRegistry(clock=clock, ttl_seconds=ttl), run_store=RunStore())


# --- registration / heartbeat ------------------------------------------------------------------


def test_register_and_heartbeat() -> None:
    reg = WorkerRegistry()
    w = reg.register("wk_1", kind=KIND_AGENT, max_slots=2)
    assert w.status == "ready" and w.max_slots == 2
    assert reg.get("wk_1").worker_id == "wk_1"
    reg.heartbeat("wk_1")
    with pytest.raises(GatewayError) as ei:
        reg.heartbeat("wk_missing")
    assert ei.value.code == "WORKER_NOT_FOUND"


# --- placement ---------------------------------------------------------------------------------


def test_place_picks_least_loaded_worker() -> None:
    coord = _coordinator()
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=2)
    coord.registry.register("wk_2", kind=KIND_AGENT, max_slots=2)
    # first two placements spread across the two idle workers.
    a = coord.place("run_1", kind=KIND_AGENT)
    b = coord.place("run_2", kind=KIND_AGENT)
    assert {a.worker_id, b.worker_id} == {"wk_1", "wk_2"}


def test_place_is_idempotent_for_a_run() -> None:
    coord = _coordinator()
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=2)
    first = coord.place("run_1", kind=KIND_AGENT)
    again = coord.place("run_1", kind=KIND_AGENT)
    assert again.worker_id == first.worker_id


def test_capacity_exhaustion_raises() -> None:
    coord = _coordinator()
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=1)
    coord.place("run_1", kind=KIND_AGENT)
    with pytest.raises(GatewayError) as ei:
        coord.place("run_2", kind=KIND_AGENT)
    assert ei.value.code == "NO_WORKER_AVAILABLE"


def test_release_frees_capacity() -> None:
    coord = _coordinator()
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=1)
    coord.place("run_1", kind=KIND_AGENT)
    coord.release("run_1")
    # after release the single slot is free again.
    assert coord.place("run_2", kind=KIND_AGENT).worker_id == "wk_1"


def test_placement_respects_kind_and_labels() -> None:
    coord = _coordinator()
    coord.registry.register("wk_browser", kind=KIND_BROWSER, max_slots=1)
    coord.registry.register("wk_gpu", kind=KIND_AGENT, max_slots=1, labels={"gpu": "yes"})
    # no agent worker without the label → no placement.
    with pytest.raises(GatewayError):
        coord.place("run_1", kind=KIND_AGENT, labels_required={"gpu": "no"})
    # the labelled worker is selected.
    assert coord.place("run_2", kind=KIND_AGENT, labels_required={"gpu": "yes"}).worker_id == "wk_gpu"
    # a browser-kind placement never lands on an agent worker.
    assert coord.place("run_3", kind=KIND_BROWSER).worker_id == "wk_browser"


# --- lease expiry / recovery -------------------------------------------------------------------


def test_stale_worker_is_reclaimed_and_live_worker_is_not() -> None:
    clock = _Clock()
    coord = _coordinator(clock=clock, ttl=30.0)
    coord.registry.register("wk_live", kind=KIND_AGENT, max_slots=1)
    coord.registry.register("wk_stale", kind=KIND_AGENT, max_slots=1)

    clock.advance(20)
    coord.registry.heartbeat("wk_live")   # keep live fresh
    clock.advance(20)                      # live: 20s ago (< ttl); stale: 40s ago (> ttl)

    lost = coord.registry.reclaim_expired()
    assert lost == ["wk_stale"]
    assert coord.registry.get("wk_stale").status == "lost"
    assert coord.registry.get("wk_live").status == "ready"


def test_recover_lost_runs_interrupts_running_placed_runs() -> None:
    clock = _Clock()
    rs = RunStore()
    coord = WorkerCoordinator(registry=WorkerRegistry(clock=clock, ttl_seconds=30.0), run_store=rs)
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=2)

    # a running run placed on the worker...
    running = rs.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    rs.transition(running.run_id, runs.PREPARING)
    rs.transition(running.run_id, runs.RUNNING)
    coord.place(running.run_id, kind=KIND_AGENT)
    # ...and a queued run placed on the same worker.
    queued = rs.create_run(agent_id="ag_2", session_id="ses_2", command_id="cmd_2")
    coord.place(queued.run_id, kind=KIND_AGENT)

    clock.advance(40)  # the worker's heartbeat lapses
    interrupted = recover_lost_runs(coord, run_store=rs)

    assert interrupted == [running.run_id]                       # only the running run is interrupted
    assert rs.get_run(running.run_id).status == runs.INTERRUPTED
    assert rs.get_run(queued.run_id).status == runs.QUEUED        # queued run freed for re-placement
    # the lost worker's placements were released, freeing capacity.
    assert coord.worker_for(running.run_id) is None


def test_placement_survives_a_fresh_coordinator_cold_read() -> None:
    coord = _coordinator()
    coord.registry.register("wk_1", kind=KIND_AGENT, max_slots=1)
    coord.place("run_1", kind=KIND_AGENT)
    # a new coordinator (cold from gateway.db) sees the durable placement.
    assert WorkerCoordinator(run_store=RunStore()).worker_for("run_1") == "wk_1"


def test_heartbeat_revives_a_lost_worker() -> None:
    clock = _Clock()
    reg = WorkerRegistry(clock=clock, ttl_seconds=30.0)
    reg.register("wk_1", kind=KIND_AGENT)
    clock.advance(40)
    assert reg.reclaim_expired() == ["wk_1"]
    reg.heartbeat("wk_1")               # the worker comes back
    assert reg.get("wk_1").status == "ready"
