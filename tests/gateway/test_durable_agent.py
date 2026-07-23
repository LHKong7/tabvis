"""Phase A — the durable Agent aggregate (design §7.2): created/refreshed atomically with each Run."""

from __future__ import annotations

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime.agents import ACTIVE, DISABLED, AgentStore, get_agent_store
from tabvis.gateway.runtime.run_store import get_run_store
from tabvis.gateway.store import db


def test_schema_is_v6_with_agents_table() -> None:
    assert db.SCHEMA_VERSION == 6
    conn = db.connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agents" in names and "runs" in names


def _agent_events(agent_id: str):
    return [e for e in get_event_store().read(aggregate_id=agent_id)]


def test_first_run_creates_durable_agent() -> None:
    store = get_run_store()
    store.create_run(
        agent_id="ag_a1", session_id="ses_1", command_id="cmd_1", model="claude-x",
        max_turns=20, profile="p1", cwd="/work", principal_id="principal_local",
    )
    agent = get_agent_store().get("ag_a1")
    assert agent is not None
    assert agent.status == ACTIVE
    assert agent.default_model == "claude-x" and agent.default_max_turns == 20
    assert agent.profile == "p1" and agent.cwd == "/work" and agent.principal_id == "principal_local"
    assert agent.created_at and agent.updated_at

    types = [e.type for e in _agent_events("ag_a1")]
    assert EventType.AGENT_CREATED in types


def test_second_run_refreshes_agent_without_duplicating() -> None:
    store = get_run_store()
    store.create_run(agent_id="ag_a2", session_id="ses_1", command_id="cmd_1", model="m1")
    created = get_agent_store().get("ag_a2")
    # a second run for the same agent (allow_concurrent to skip the one-active guard for the test).
    store.create_run(agent_id="ag_a2", session_id="ses_1", command_id="cmd_2", model="m2", allow_concurrent=True)

    agents = get_agent_store().list()
    assert len([a for a in agents if a.agent_id == "ag_a2"]) == 1  # one durable row, not two
    refreshed = get_agent_store().get("ag_a2")
    assert refreshed.created_at == created.created_at          # identity created_at preserved
    assert refreshed.default_model == "m2"                    # defaults refreshed
    types = [e.type for e in _agent_events("ag_a2")]
    assert EventType.AGENT_CREATED in types and EventType.AGENT_UPDATED in types


def test_zero_run_agent_and_lifecycle() -> None:
    # An agent can exist and be listed without ever having a run is covered once a run creates it;
    # here we exercise the lifecycle transition on a created agent.
    get_run_store().create_run(agent_id="ag_a3", session_id="ses_1", command_id="cmd_1")
    store = AgentStore()
    disabled = store.set_status("ag_a3", DISABLED)
    assert disabled.status == DISABLED
    assert get_agent_store().get("ag_a3").status == DISABLED
    assert EventType.AGENT_DISABLED in [e.type for e in _agent_events("ag_a3")]
    # listing filtered to active excludes the disabled agent.
    assert "ag_a3" not in {a.agent_id for a in store.list(statuses=(ACTIVE,))}


# --- Phase B: "durable Agent + latest Run" projection ------------------------------------------


def test_projection_merges_durable_agent() -> None:
    from tabvis.gateway.protocol.compatibility import project_agent_only, project_run_as_agent

    store = get_run_store()
    run = store.create_run(agent_id="ag_b1", session_id="ses", command_id="cmd", model="m",
                           profile="pf", cwd="/w", principal_id="principal_local")
    agent = get_agent_store().get("ag_b1").to_dict()
    view = project_run_as_agent(run, agent)
    # legacy execution keys preserved
    assert view["status"] == "queued" and view["run_id"] == run.run_id and view["latest_run"]["run_id"] == run.run_id
    # durable half merged additively
    assert view["agent_status"] == ACTIVE and view["profile"] == "pf" and view["default_model"] == "m"
    assert view["agent_created_at"] == agent["created_at"]

    # a zero-run agent projects a neutral queued view with no latest_run
    zero = project_agent_only(agent)
    assert zero["status"] == "queued" and zero["latest_run"] is None and zero["agent_status"] == ACTIVE


def test_http_agent_view_and_zero_run_agent() -> None:
    from starlette.testclient import TestClient

    from tabvis.gateway.access.http import create_gateway_app

    app = create_gateway_app()
    run = app.state.gateway.runs.create_run(agent_id="ag_http", session_id="ses", command_id="cmd",
                                            model="m", profile="pf")
    with TestClient(app) as client:
        view = client.get("/v1/agents/ag_http").json()
        assert view["agent_id"] == "ag_http" and view["status"] == "queued"
        assert view["agent_status"] == ACTIVE and view["profile"] == "pf"
        assert view["latest_run"]["run_id"] == run.run_id
        # the merged list carries the durable fields too
        listing = client.get("/v1/agents").json()
        assert listing["count"] == 1 and listing["agents"][0]["agent_status"] == ACTIVE
        # a truly unknown agent is still 404
        assert client.get("/v1/agents/ag_nope").status_code == 404
