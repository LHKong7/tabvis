"""Phase D — migrating legacy AgentRecord envelopes into the durable Agent + Run stores."""

from __future__ import annotations

import json
import os

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agents import ACTIVE, get_agent_store
from tabvis.gateway.runtime.legacy_migration import migrate_legacy_agents
from tabvis.gateway.store import db
from tabvis.utils.env_utils import get_tabvis_config_home_dir


def _write_legacy(agent_id: str, run_id: str, status: str, **overrides) -> None:
    record = {
        "agent_id": agent_id, "session_id": "ses_old", "status": status, "run_id": run_id,
        "principal_id": "principal_local", "model": "claude-legacy", "max_turns": 30,
        "profile": "pf_old", "cwd": "/old/work",
        "created_at": "2026-01-01T00:00:00+00:00", "started_at": "2026-01-01T00:00:01+00:00",
        "ended_at": "2026-01-01T00:00:05+00:00", "turns": 3, "tool_calls": 2,
        "result": "the old answer", "error": None, "is_error": False, "browser": {}, "duration_ms": 4000,
    }
    record.update(overrides)
    agents_dir = os.path.join(get_tabvis_config_home_dir(), "agents")
    os.makedirs(agents_dir, exist_ok=True)
    with open(os.path.join(agents_dir, f"{agent_id}.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh)


def test_migrate_creates_durable_agent_and_run() -> None:
    _write_legacy("ag_legacy1", "run_old1", "completed")
    result = migrate_legacy_agents()
    assert result["migrated"] == 1 and "ag_legacy1" in result["agent_ids"]

    agent = get_agent_store().get("ag_legacy1")
    assert agent is not None and agent.status == ACTIVE
    assert agent.profile == "pf_old" and agent.default_model == "claude-legacy" and agent.default_max_turns == 30
    assert agent.created_at == "2026-01-01T00:00:00+00:00"  # durable identity created_at preserved
    assert agent.principal_id == "principal_local"

    run = db.get_run("run_old1")
    assert run is not None and run["status"] == runs.COMPLETED
    assert run["agent_id"] == "ag_legacy1" and run["turns"] == 3 and run["command_id"].startswith("cmd_migrated_")

    # the legacy result text is carried onto the run.completed event (the run row has no result column).
    completed = [e for e in get_event_store().read(aggregate_id="run_old1") if e.type == EventType.RUN_COMPLETED]
    assert completed and completed[0].data.get("result_preview") == "the old answer"


def test_migrate_maps_crashed_status_and_is_idempotent() -> None:
    _write_legacy("ag_legacy2", "run_old2", "running")  # a crashed legacy run
    first = migrate_legacy_agents()
    assert first["migrated"] == 1
    run = db.get_run("run_old2")
    assert run["status"] == runs.INTERRUPTED  # running -> interrupted (honest "process gone")

    # re-running migrates nothing new and creates no duplicate.
    second = migrate_legacy_agents()
    assert second["migrated"] == 0 and second["skipped"] >= 1
    assert len([a for a in get_agent_store().list() if a.agent_id == "ag_legacy2"]) == 1


def test_migrate_no_legacy_data_is_noop() -> None:
    assert migrate_legacy_agents() == {"migrated": 0, "skipped": 0, "agent_ids": []}
