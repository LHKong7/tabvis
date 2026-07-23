"""Registry-retirement cutover: the gateway serves the legacy /agents surface (design §9.8).

The gateway-backed handlers are exercised two ways: an isolated app (with an injected fake launcher, so
POST /agent runs to completion without a model) proving the full lifecycle, and the real server with
TABVIS_GATEWAY_AGENTS=1 proving the flag flips the read path onto gateway Run data.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tabvis.gateway.access.legacy_agents import build_legacy_agent_routes
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.runtime.agent.runner import AgentRunLauncher
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.run_store import RunStore


def _fake_stream(messages):
    async def stream(run, context):
        for m in messages:
            yield m

    return stream


def _app_with_fake_launcher(messages):
    launcher = AgentRunLauncher(stream_fn=_fake_stream(messages))
    gw = GatewayApplication.build(launcher=launcher)
    gw.startup()
    app = Starlette(routes=build_legacy_agent_routes())
    app.state.gateway = gw
    return app, gw


def _msgs():
    return [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}},
        {"type": "result", "result": "all done"},
    ]


# --- isolated app: full lifecycle through the gateway ------------------------------------------


def test_post_agent_streams_legacy_frames_from_a_gateway_run() -> None:
    app, gw = _app_with_fake_launcher(_msgs())
    client = TestClient(app)
    resp = client.post("/agent", json={"prompt": "do it"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert resp.headers.get("x-agent-id", "").startswith("ag_")
    text = resp.text
    # the run's v1 events projected to legacy frame names.
    assert "event: agent" in text
    assert "event: assistant" in text
    assert "event: result" in text and "event: done" in text


def test_post_agent_then_list_and_read_from_gateway() -> None:
    app, gw = _app_with_fake_launcher(_msgs())
    client = TestClient(app)
    agent_id = client.post("/agent", json={"prompt": "go"}).headers["x-agent-id"]

    listing = client.get("/agents").json()
    assert listing["count"] == 1 and listing["agents"][0]["agent_id"] == agent_id
    detail = client.get(f"/agents/{agent_id}")
    assert detail.status_code == 200 and detail.json()["agent_id"] == agent_id
    assert detail.json()["status"] == "completed"


def test_post_agent_requires_prompt() -> None:
    app, _ = _app_with_fake_launcher(_msgs())
    resp = TestClient(app).post("/agent", json={})
    assert resp.status_code == 400


def test_reuse_unknown_agent_id_is_404() -> None:
    app, _ = _app_with_fake_launcher(_msgs())
    resp = TestClient(app).post("/agent", json={"prompt": "go", "agent_id": "ag_nope"})
    assert resp.status_code == 404


def test_capacity_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SERVER_MAX_AGENTS", "1")
    # seed one active (queued) run so the gateway is already at capacity.
    RunStore().create_run(agent_id="ag_busy", session_id="ses", command_id="cmd_busy")
    app, _ = _app_with_fake_launcher(_msgs())
    resp = TestClient(app).post("/agent", json={"prompt": "go"})
    assert resp.status_code == 429
    assert resp.json()["max_agents"] == 1


def test_cancel_agent_through_the_gateway() -> None:
    # a queued run (no launcher execution) is cancellable via the legacy cancel path.
    app, gw = _app_with_fake_launcher(_msgs())
    RunStore().create_run(agent_id="ag_c", session_id="ses_c", command_id="cmd_c")
    resp = TestClient(app).post("/agents/ag_c/cancel")
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"


# --- real server, flag on: the read path is gateway-backed -------------------------------------


def test_server_flag_routes_agents_to_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_GATEWAY_AGENTS", "1")
    from tabvis.browser.server import create_app

    # seed a gateway run (the registry is empty) — proving GET /agents reads gateway data, not registry.
    rs = RunStore()
    run = rs.create_run(agent_id="ag_gw", session_id="ses_gw", command_id="cmd_gw", model="m")
    rs.transition(run.run_id, runs.PREPARING)
    rs.transition(run.run_id, runs.RUNNING)
    rs.transition(run.run_id, runs.COMPLETED)

    client = TestClient(create_app(auth_required=False))
    body = client.get("/v1/agents").json()
    assert any(a["agent_id"] == "ag_gw" for a in body["agents"])   # served from gateway Run data
    detail = client.get("/v1/agents/ag_gw").json()
    assert detail["status"] == "completed" and detail["run_id"] == run.run_id


def test_registry_only_agent_is_invisible_after_retirement(monkeypatch: pytest.MonkeyPatch) -> None:
    # Phase 6 convergence: the AgentRecord registry is retired from the public path. An agent that
    # exists ONLY in the legacy registry (no durable gateway Agent/Run) is not served by /agents.
    monkeypatch.delenv("TABVIS_GATEWAY_AGENTS", raising=False)
    from tabvis.agent.agents import registry as reg
    from tabvis.browser.server import create_app

    reg.create(agent_id="ag_reg", session_id="s", prompt="p")
    client = TestClient(create_app(auth_required=False))
    body = client.get("/v1/agents").json()
    assert not any(a["agent_id"] == "ag_reg" for a in body["agents"])   # registry is off the public path
