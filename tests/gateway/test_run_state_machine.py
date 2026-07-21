"""Phase 1 — the Run state machine (design §7.4, §16.2 invariants)."""

from __future__ import annotations

import pytest

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.runs import RunRecord


def test_declared_edges_are_allowed() -> None:
    assert runs.can_transition(runs.QUEUED, runs.PREPARING)
    assert runs.can_transition(runs.PREPARING, runs.RUNNING)
    assert runs.can_transition(runs.RUNNING, runs.WAITING_FOR_INPUT)
    assert runs.can_transition(runs.WAITING_FOR_INPUT, runs.RUNNING)
    assert runs.can_transition(runs.RUNNING, runs.COMPLETED)
    assert runs.can_transition(runs.CANCELLING, runs.CANCELLED)


def test_undeclared_edges_are_rejected() -> None:
    assert not runs.can_transition(runs.QUEUED, runs.RUNNING)  # must go through preparing
    assert not runs.can_transition(runs.RUNNING, runs.QUEUED)
    assert not runs.can_transition(runs.COMPLETED, runs.RUNNING)


@pytest.mark.parametrize("terminal", sorted(runs.TERMINAL))
def test_terminal_states_never_transition(terminal: str) -> None:
    # §16.2 invariant: terminal Run states never transition.
    for dst in (runs.RUNNING, runs.QUEUED, runs.COMPLETED, runs.FAILED):
        assert not runs.can_transition(terminal, dst)


def test_assert_transition_raises_on_illegal_edge() -> None:
    with pytest.raises(GatewayError) as ei:
        runs.assert_transition(runs.COMPLETED, runs.RUNNING)
    assert ei.value.code == "INVALID_STATE_TRANSITION"


def test_record_flags() -> None:
    rec = RunRecord(run_id="run_1", agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
    assert rec.status == runs.QUEUED
    assert rec.is_active and not rec.is_terminal and not rec.is_waiting
    rec.status = runs.WAITING_FOR_INPUT
    assert rec.is_waiting and rec.is_active
    rec.status = runs.COMPLETED
    assert rec.is_terminal and not rec.is_active


def test_record_round_trips_through_dict() -> None:
    rec = RunRecord(run_id="run_1", agent_id="ag_1", session_id="ses_1", command_id="cmd_1", turns=3)
    assert RunRecord.from_dict(rec.to_dict()).to_dict() == rec.to_dict()
    # unknown keys (e.g. a future field) are ignored, not fatal.
    assert RunRecord.from_dict({**rec.to_dict(), "future_field": 1}).turns == 3
