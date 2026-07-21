"""Phase 1 — RunStore: create/transition, CAS, one-active-run, event emission (design §7, §12.3, §15)."""

from __future__ import annotations

import pytest

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.run_store import RunStore


def _finish(store: RunStore, run_id: str) -> None:
    store.transition(run_id, runs.PREPARING)
    store.transition(run_id, runs.RUNNING)
    store.transition(run_id, runs.COMPLETED)


def test_create_run_persists_and_emits_run_created() -> None:
    store = RunStore()
    run = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", model="m")
    assert run.run_id.startswith("run_")
    assert store.get_run(run.run_id).status == runs.QUEUED
    events = get_event_store().read(aggregate_id=run.run_id)
    assert [e.type for e in events] == [EventType.RUN_CREATED]
    assert events[0].scope.agent_id == "ag_1"
    assert events[0].correlation_id == "cmd_1"


def test_second_active_run_for_same_agent_is_rejected() -> None:
    store = RunStore()
    store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    with pytest.raises(GatewayError) as ei:
        store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_2")
    assert ei.value.code == "RUN_ALREADY_ACTIVE"


def test_continuing_one_agent_yields_two_queryable_runs() -> None:
    # design §15 Phase 1 acceptance: continuing an Agent creates two queryable Runs.
    store = RunStore()
    first = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    _finish(store, first.run_id)  # first run reaches a terminal state
    second = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_2", attempt=2)
    _finish(store, second.run_id)

    history = store.list_runs_for_agent("ag_1")
    assert {r.run_id for r in history} == {first.run_id, second.run_id}
    assert len(history) == 2
    # each run kept its own immutable identity and command.
    assert {r.command_id for r in history} == {"cmd_1", "cmd_2"}


def test_transition_emits_the_matching_event_and_stamps_timing() -> None:
    store = RunStore()
    run = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    store.transition(run.run_id, runs.PREPARING)
    running = store.transition(run.run_id, runs.RUNNING)
    assert running.started_at is not None
    done = store.transition(run.run_id, runs.COMPLETED, result_message_id="msg_1")
    assert done.ended_at is not None
    assert done.result_message_id == "msg_1"

    types = [e.type for e in get_event_store().read(aggregate_id=run.run_id)]
    assert types == [
        EventType.RUN_CREATED,
        "run.preparing",
        EventType.RUN_STARTED,
        EventType.RUN_COMPLETED,
    ]
    # per-aggregate seq is strictly increasing across the run's whole lifecycle (design §5.5).
    seqs = [e.seq for e in get_event_store().read(aggregate_id=run.run_id)]
    assert seqs == [1, 2, 3, 4]


def test_transition_cas_rejects_wrong_expected_state() -> None:
    store = RunStore()
    run = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    with pytest.raises(GatewayError) as ei:
        store.transition(run.run_id, runs.RUNNING, expected=runs.RUNNING)  # it's actually queued
    assert ei.value.code == "CONFLICT"
    # the run was not mutated by the rejected transition.
    assert store.get_run(run.run_id).status == runs.QUEUED


def test_illegal_transition_is_rejected() -> None:
    store = RunStore()
    run = store.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    with pytest.raises(GatewayError) as ei:
        store.transition(run.run_id, runs.COMPLETED)  # queued -> completed is not an edge
    assert ei.value.code == "INVALID_STATE_TRANSITION"


def test_transition_on_unknown_run() -> None:
    store = RunStore()
    with pytest.raises(GatewayError) as ei:
        store.transition("run_missing", runs.PREPARING)
    assert ei.value.code == "RUN_NOT_FOUND"


def test_terminal_run_history_survives_a_fresh_store_read() -> None:
    # a new RunStore instance (cold read from gateway.db) still sees terminal runs — durability.
    first = RunStore()
    run = first.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    _finish(first, run.run_id)
    reread = RunStore().get_run(run.run_id)
    assert reread is not None and reread.status == runs.COMPLETED and reread.ended_at
