"""Phase A — the durable Agent aggregate (design §7.2): created/refreshed atomically with each Run."""

from __future__ import annotations

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime.agents import ACTIVE, DISABLED, AgentStore, get_agent_store
from tabvis.gateway.runtime.run_store import get_run_store
from tabvis.gateway.store import db


def test_schema_is_v8_with_agents_run_results_and_scheduled_tasks_tables() -> None:
    assert db.SCHEMA_VERSION == 8
    conn = db.connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agents", "runs", "run_results", "scheduled_tasks"} <= names


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


def test_disabled_or_deleted_agent_cannot_start_new_run() -> None:
    # Bug #6: create_run must gate on the durable Agent lifecycle. A disabled agent is a CONFLICT; a
    # deleted one is NOT_FOUND. A brand-new agent (first run) is never gated.
    import pytest

    from tabvis.gateway.protocol.errors import GatewayError
    from tabvis.gateway.runtime.agents import DELETED, DISABLED, get_agent_store

    store = get_run_store()
    store.create_run(agent_id="ag_gate", session_id="s", command_id="c1")  # creates ACTIVE agent
    get_agent_store().set_status("ag_gate", DISABLED)
    with pytest.raises(GatewayError) as disabled_err:
        store.create_run(agent_id="ag_gate", session_id="s", command_id="c2", allow_concurrent=True)
    assert disabled_err.value.code == "CONFLICT"

    get_agent_store().set_status("ag_gate", DELETED)
    with pytest.raises(GatewayError) as deleted_err:
        store.create_run(agent_id="ag_gate", session_id="s", command_id="c3", allow_concurrent=True)
    assert deleted_err.value.code == "NOT_FOUND"


def test_noop_refresh_emits_no_agent_updated() -> None:
    # Bug #15: a refresh that changes no durable field (the common case — same agent, same config, next
    # run) must not churn the row or fan out an agent.updated event.
    store = get_run_store()
    store.create_run(agent_id="ag_same", session_id="s", command_id="c1", model="m", profile="p")
    store.create_run(agent_id="ag_same", session_id="s", command_id="c2", model="m", profile="p",
                     allow_concurrent=True)
    types = [e.type for e in _agent_events("ag_same")]
    assert types.count(EventType.AGENT_CREATED) == 1
    assert EventType.AGENT_UPDATED not in types  # nothing durable changed → no update event


def test_list_agents_limit_zero_and_negative_return_all() -> None:
    # Bug #13: a 0 or negative ?limit is meaningless and must not truncate/slice-from-end.
    from starlette.testclient import TestClient

    from tabvis.gateway.access.http import create_gateway_app

    app = create_gateway_app()
    rs = app.state.gateway.runs
    rs.create_run(agent_id="ag_lim1", session_id="s", command_id="c1")
    rs.create_run(agent_id="ag_lim2", session_id="s", command_id="c2")
    with TestClient(app) as client:
        assert client.get("/v1/agents?limit=0").json()["count"] == 2
        assert client.get("/v1/agents?limit=-1").json()["count"] == 2
        assert client.get("/v1/agents?limit=1").json()["count"] == 1


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
    with TestClient(app) as client:
        # Created inside the lifespan: boot retires runs left non-terminal by a previous process,
        # and a run cannot predate the process that serves it.
        run = app.state.gateway.runs.create_run(agent_id="ag_http", session_id="ses",
                                                command_id="cmd", model="m", profile="pf")
        view = client.get("/v1/agents/ag_http").json()
        assert view["agent_id"] == "ag_http" and view["status"] == "queued"
        assert view["agent_status"] == ACTIVE and view["profile"] == "pf"
        assert view["latest_run"]["run_id"] == run.run_id
        # the merged list carries the durable fields too
        listing = client.get("/v1/agents").json()
        assert listing["count"] == 1 and listing["agents"][0]["agent_status"] == ACTIVE
        # a truly unknown agent is still 404
        assert client.get("/v1/agents/ag_nope").status_code == 404
