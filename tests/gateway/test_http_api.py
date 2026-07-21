"""Phase 3 — the gateway HTTP surface end to end (design §9.4, §9.5, §15 acceptance)."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tabvis.gateway.access.http import create_gateway_app
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.runtime import runs


def _app_and_gateway(host: str = "127.0.0.1"):
    gw = GatewayApplication.build(host=host)
    return create_gateway_app(gw), gw


def test_health_reports_components_and_capacity() -> None:
    app, _ = _app_and_gateway()
    body = TestClient(app).get("/v1/health").json()
    assert body["status"] == "degraded"  # no launcher wired → control-plane-only
    assert body["components"]["metadata_store"] == "ready"
    assert body["components"]["agent_runtime"] == "not_configured"
    assert body["capacity"]["runs"] == 4


def test_create_run_returns_202_and_is_readable() -> None:
    app, _ = _app_and_gateway()
    client = TestClient(app)
    resp = client.post("/v1/runs", json={"model": "m"})
    assert resp.status_code == 202
    run = resp.json()["data"]["run"]
    assert run["run_id"].startswith("run_")
    assert run["status"] == runs.QUEUED

    got = client.get(f"/v1/runs/{run['run_id']}")
    assert got.status_code == 200
    assert got.json()["run"]["run_id"] == run["run_id"]


def test_create_run_is_idempotent_on_command_id() -> None:
    app, _ = _app_and_gateway()
    client = TestClient(app)
    headers = {"x-tabvis-command-id": "cmd_fixed"}
    first = client.post("/v1/runs", json={"model": "m"}, headers=headers).json()
    second = client.post("/v1/runs", json={"model": "m"}, headers=headers).json()
    assert first["data"]["run"]["run_id"] == second["data"]["run"]["run_id"]
    assert second["duplicate"] is True


def test_read_unknown_run_is_404() -> None:
    app, _ = _app_and_gateway()
    resp = TestClient(app).get("/v1/runs/run_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_cancel_run_over_http() -> None:
    app, _ = _app_and_gateway()
    client = TestClient(app)
    run = client.post("/v1/runs", json={}).json()["data"]["run"]
    resp = client.post(f"/v1/runs/{run['run_id']}/cancel")
    assert resp.status_code == 200
    assert resp.json()["data"]["run"]["status"] == runs.CANCELLED


def test_respond_interaction_over_http_resumes_the_run() -> None:
    app, gw = _app_and_gateway()
    client = TestClient(app)
    # Drive a run to running and raise a question directly through the wired services.
    run = client.post("/v1/runs", json={}).json()["data"]["run"]
    gw.runs.transition(run["run_id"], runs.PREPARING)
    gw.runs.transition(run["run_id"], runs.RUNNING)
    interaction = gw.interactions.request(run["run_id"], "question", {"text": "Which env?"})

    resp = client.post(
        f"/v1/interactions/{interaction.interaction_id}/responses",
        json={"answers": {"choice": "prod"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["interaction"]["status"] == "answered"
    assert gw.runs.get_run(run["run_id"]).status == runs.RUNNING


def test_validation_error_has_stable_code_and_shape() -> None:
    app, _ = _app_and_gateway()
    resp = TestClient(app).post("/v1/runs", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["error"]["retryable"] is False


def test_events_catch_up_replays_durable_backlog() -> None:
    app, _ = _app_and_gateway()
    client = TestClient(app)
    run = client.post("/v1/runs", json={}).json()["data"]["run"]
    # follow=0 → a bounded catch-up stream that ends after the durable backlog (design §9.5).
    resp = client.get("/v1/events", params={"run_id": run["run_id"], "follow": "0"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    assert "event: run.created" in resp.text


def test_events_resume_from_cursor_has_no_gap_or_duplicate() -> None:
    app, gw = _app_and_gateway()
    client = TestClient(app)
    run = client.post("/v1/runs", json={}).json()["data"]["run"]
    gw.runs.transition(run["run_id"], runs.PREPARING)  # a second event on this run

    # Grab the first event's cursor, then resume after it.
    first_batch = gw.events.read(aggregate_id=run["run_id"])
    resume_after = first_batch[0].cursor
    resp = client.get(
        "/v1/events", params={"run_id": run["run_id"], "follow": "0", "cursor": f"{resume_after:016d}"}
    )
    # only events strictly after the cursor come back (run.created is excluded).
    assert "event: run.created" not in resp.text
    assert "event: run.preparing" in resp.text


def test_security_headers_present() -> None:
    app, _ = _app_and_gateway()
    resp = TestClient(app).get("/v1/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


def test_non_loopback_bind_requires_auth() -> None:
    app, _ = _app_and_gateway(host="0.0.0.0")  # public bind → auth required
    resp = TestClient(app).get("/v1/runs/run_x")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHENTICATED"


def test_admin_token_authorizes_non_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SERVER_ADMIN_TOKEN", "s3cret")
    app, _ = _app_and_gateway(host="0.0.0.0")
    client = TestClient(app)
    assert client.get("/v1/runs/run_x").status_code == 401
    # with the admin bearer token the same request authenticates (then 404 for the missing run).
    resp = client.get("/v1/runs/run_x", headers={"authorization": "Bearer s3cret"})
    assert resp.status_code == 404
