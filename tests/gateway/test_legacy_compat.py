"""Legacy /agents compatibility projection (design §9.8).

The gateway can serve the legacy agent-centric surface entirely from its Run data — a projection, not a
second lifecycle. Unit tests cover the mapping; integration tests drive the standalone gateway app
(which mounts the compat routes) and assert the legacy shapes.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tabvis.gateway.access.http import create_gateway_app
from tabvis.gateway.auth.principals import agent_principal, local_admin
from tabvis.gateway.protocol import compatibility as compat
from tabvis.gateway.protocol.events import AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.runs import RunRecord


# --- projection units --------------------------------------------------------------------------


def test_legacy_status_mapping() -> None:
    assert compat.legacy_status(runs.PREPARING) == "running"
    assert compat.legacy_status(runs.WAITING_FOR_INPUT) == "running"
    assert compat.legacy_status(runs.INTERRUPTED) == "failed"
    assert compat.legacy_status(runs.COMPLETED) == "completed"
    assert compat.legacy_status(runs.CANCELLED) == "cancelled"


def test_project_run_as_agent_shape() -> None:
    run = RunRecord(run_id="run_1", agent_id="ag_1", session_id="ses_1", command_id="cmd_1",
                    model="m", status=runs.RUNNING, turns=3, tool_calls=2)
    view = compat.project_run_as_agent(run)
    assert view["agent_id"] == "ag_1" and view["session_id"] == "ses_1"
    assert view["status"] == "running" and view["run_id"] == "run_1"
    assert view["turns"] == 3 and view["tool_calls"] == 2
    assert view["latest_run"]["run_id"] == "run_1"  # full gateway record embedded


def test_legacy_frames_for_maps_lifecycle_events() -> None:
    def ev(t, data=None):
        return __import__("tabvis.gateway.protocol.events", fromlist=["EventEnvelope"]).EventEnvelope(
            event_id="e", cursor=1, aggregate_type=AGGREGATE_RUN, aggregate_id="run_1", seq=1,
            type=t, scope=EventScope(run_id="run_1"), data=data or {},
        )

    assert compat.legacy_frames_for(ev(EventType.RUN_CREATED, {"agent_id": "ag_1"}))[0]["event"] == "agent"
    assert compat.legacy_frames_for(ev(EventType.ASSISTANT_MESSAGE_COMPLETED, {"text_preview": "hi"}))[0]["event"] == "assistant"
    assert compat.legacy_frames_for(ev(EventType.TOOL_COMPLETED))[0]["event"] == "tool_use"
    completed = [f["event"] for f in compat.legacy_frames_for(ev(EventType.RUN_COMPLETED, {"result_preview": "x"}))]
    assert completed == ["result", "done"]
    assert compat.legacy_frames_for(ev(EventType.RUN_STARTED)) == []  # not a legacy frame


def test_principal_ownership_filter() -> None:
    admin = local_admin()
    agent = agent_principal("ag_1")
    assert admin.can_access_agent("ag_x") is True             # admin sees all
    assert agent.can_access_agent("ag_1") is True             # own agent
    assert agent.can_access_agent("ag_2") is False            # not others


# --- integration (standalone gateway app serves the legacy surface) ----------------------------


@pytest.fixture()
def seeded():
    """Two agents in the gateway store: ag_a completed, ag_b queued (active)."""
    from tabvis.gateway.runtime.run_store import RunStore

    rs = RunStore()
    a = rs.create_run(agent_id="ag_a", session_id="ses_a", command_id="cmd_a", model="m")
    rs.transition(a.run_id, runs.PREPARING)
    rs.transition(a.run_id, runs.RUNNING)
    rs.transition(a.run_id, runs.COMPLETED)
    rs.create_run(agent_id="ag_b", session_id="ses_b", command_id="cmd_b", model="m")  # queued
    return TestClient(create_gateway_app())


def test_list_agents_projects_latest_run_per_agent(seeded: TestClient) -> None:
    body = seeded.get("/v1/agents").json()
    assert body["count"] == 2
    statuses = {a["agent_id"]: a["status"] for a in body["agents"]}
    assert statuses == {"ag_a": "completed", "ag_b": "queued"}


def test_list_agents_status_filter(seeded: TestClient) -> None:
    body = seeded.get("/v1/agents", params={"status": "completed"}).json()
    assert body["count"] == 1 and body["agents"][0]["agent_id"] == "ag_a"


def test_read_agent_detail_and_unknown(seeded: TestClient) -> None:
    ok = seeded.get("/v1/agents/ag_a")
    assert ok.status_code == 200 and ok.json()["status"] == "completed" and ok.json()["run_id"]
    missing = seeded.get("/v1/agents/ag_zzz")
    assert missing.status_code == 404 and missing.json()["error"] == "unknown agent_id"


def test_cancel_active_agent_and_terminal_conflict(seeded: TestClient) -> None:
    # ag_b is queued (active) → cancel succeeds and projects the cancelled status.
    resp = seeded.post("/v1/agents/ag_b/cancel")
    assert resp.status_code == 200 and resp.json() == {"agent_id": "ag_b", "status": "cancelled"}
    assert seeded.get("/v1/agents/ag_b").json()["status"] == "cancelled"
    # ag_a already completed → cancelling is a 409 conflict.
    conflict = seeded.post("/v1/agents/ag_a/cancel")
    assert conflict.status_code == 409 and conflict.json()["status"] == "completed"


def test_agent_events_projected_to_legacy_frames(seeded: TestClient) -> None:
    resp = seeded.get("/v1/agents/ag_a/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    # the completed run's v1 events project to legacy 'agent' + 'result'/'done' frames.
    assert "event: agent" in resp.text
    assert "event: result" in resp.text and "event: done" in resp.text
