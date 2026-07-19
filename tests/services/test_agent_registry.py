"""Tests for ``tabvis.agent.agents.registry`` — agents as durable, reusable entities.

An agent has a stable ``agent_id`` and ``session_id`` and is meant to be re-run (``reuse``) many
times, keeping its session (transcript continues) and its ``profile`` (same bundled browser). Records
are mirrored to ``<config-home>/agents/<id>.json`` and loaded back on restart so the id survives.

The registry uses module globals, so a fixture clears them per test. ``config_home`` (autouse from
tests/conftest.py) points the agents dir at a tmp dir, so disk round-trips never touch the real one.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from tabvis.agent.agents import registry as r


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False
    yield
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False


# --------------------------------------------------------------------------- reuse


def test_reuse_keeps_identity_and_resets_run_fields() -> None:
    rec = r.create(
        agent_id="ag_stable",
        session_id="sess-1",
        prompt="first",
        model="m1",
        profile="work",
        cwd="/repo",
    )
    # Simulate a finished run that accrued state.
    rec.status = "completed"
    rec.turns = 5
    rec.tool_calls = 9
    rec.result = "done"
    rec.started_at = "t0"
    rec.ended_at = "t1"

    again = r.reuse("ag_stable", prompt="second", max_turns=42)

    assert again is rec  # same object — the durable entity, re-armed
    # identity kept
    assert again.agent_id == "ag_stable"
    assert again.session_id == "sess-1"     # session continues
    assert again.profile == "work"          # same bundled browser
    assert again.cwd == "/repo"
    assert again.model == "m1"              # not overridden -> kept
    # run-scoped fields reset for the new run
    assert again.status == "queued"
    assert again.prompt == "second"
    assert again.max_turns == 42
    assert again.turns == 0 and again.tool_calls == 0
    assert again.result is None and again.error is None and again.is_error is False
    assert again.started_at is None and again.ended_at is None


def test_reuse_unknown_agent_returns_none() -> None:
    assert r.reuse("nope", prompt="x") is None


def test_reuse_can_override_model() -> None:
    r.create(agent_id="ag1", session_id="s", prompt="p", model="old")
    again = r.reuse("ag1", prompt="p2", model="new")
    assert again is not None and again.model == "new"


def test_reuse_sets_resume_but_create_does_not() -> None:
    """A reused agent replays its session (resume=True); a fresh agent starts blank (resume=False).

    ``resume`` is a per-run input, never part of the persisted record."""
    fresh = r.create(agent_id="ag_new", session_id="s", prompt="p")
    assert fresh.resume is False
    assert "resume" not in fresh.to_dict()

    again = r.reuse("ag_new", prompt="p2")
    assert again is not None and again.resume is True
    assert "resume" not in again.to_dict()


# --------------------------------------------------------------------------- durability (restart)


def _write_record_to_disk(data: dict[str, Any]) -> None:
    os.makedirs(r.agents_dir(), exist_ok=True)
    with open(r.record_path(data["agent_id"]), "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def test_load_persisted_agents_round_trips() -> None:
    _write_record_to_disk(
        {
            "agent_id": "ag_persisted",
            "session_id": "sess-9",
            "status": "completed",
            "prompt": "hello",
            "profile": "research",
            "cwd": "/repo",
            "duration_ms": 1234,  # computed key that isn't a constructor field — must be ignored
        }
    )
    n = r.load_persisted_agents()
    assert n == 1
    rec = r.get("ag_persisted")
    assert rec is not None
    assert rec.session_id == "sess-9"
    assert rec.profile == "research"
    assert rec.status == "completed"
    # ...and it can be reused (session + profile intact) after a "restart".
    again = r.reuse("ag_persisted", prompt="again")
    assert again is not None and again.session_id == "sess-9" and again.profile == "research"


def test_persisted_running_is_normalized_to_failed() -> None:
    """A record left as running when the process died has no task — it must not read as running."""
    _write_record_to_disk(
        {"agent_id": "ag_crashed", "session_id": "s", "status": "running", "prompt": "p"}
    )
    r.load_persisted_agents()
    rec = r.get("ag_crashed")
    assert rec is not None
    assert rec.status == "failed"
    assert rec.error  # a reason is filled in
    # And it is reusable (a stale-running agent never blocks a new run).
    assert r.reuse("ag_crashed", prompt="retry") is not None


def test_load_does_not_clobber_live_records() -> None:
    live = r.create(agent_id="ag_dup", session_id="live", prompt="p")
    _write_record_to_disk({"agent_id": "ag_dup", "session_id": "stale", "status": "completed", "prompt": "old"})
    r.load_persisted_agents()
    assert r.get("ag_dup") is live  # in-memory wins; disk copy ignored


def test_get_and_list_trigger_lazy_load() -> None:
    _write_record_to_disk({"agent_id": "ag_lazy", "session_id": "s", "status": "completed", "prompt": "p"})
    assert r._persisted_loaded is False
    # first access loads from disk
    assert r.get("ag_lazy") is not None
    assert r._persisted_loaded is True
    assert any(a.agent_id == "ag_lazy" for a in r.list_agents())
