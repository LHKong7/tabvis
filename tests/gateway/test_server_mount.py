"""Mounting the gateway into the live daemon (integration).

Drives the real `tabvis.browser.server.create_app` and asserts the Agent Gateway control plane is
mounted alongside the legacy API — the gateway's /v1 command surface answers, the launcher is wired
(agent_runtime ready), and the legacy endpoints are untouched. Actual run execution is covered by
`test_agent_launcher.py` with an injected loop; here we avoid POST /v1/runs so no real model/browser
is launched.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tabvis.browser.server import create_app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(auth_required=False))


def test_gateway_health_is_mounted_and_launcher_is_wired(client: TestClient) -> None:
    resp = client.get("/v1/gateway/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["protocol"] == "tabvis.gateway.v1"
    assert body["status"] == "ready"                       # a launcher is wired in the daemon
    assert body["components"]["agent_runtime"] == "ready"


def test_legacy_health_is_untouched(client: TestClient) -> None:
    # the legacy /v1/health still answers and is NOT the gateway body.
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    assert resp.json().get("protocol") != "tabvis.gateway.v1"


def test_legacy_agents_endpoint_still_works(client: TestClient) -> None:
    assert client.get("/v1/agents").status_code == 200


def test_gateway_run_routes_answer_through_the_mounted_app(client: TestClient) -> None:
    # routing + error-body wiring work through the real server (loopback → local admin, so 404 not 401).
    resp = client.get("/v1/runs/run_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_gateway_validation_error_through_the_mounted_app(client: TestClient) -> None:
    resp = client.post("/v1/runs", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "VALIDATION_FAILED"


def test_gateway_events_sse_catch_up_is_mounted(client: TestClient) -> None:
    # GET /v1/events (SSE) coexists with the legacy WebSocket at the same path; follow=0 → bounded.
    resp = client.get("/v1/events", params={"follow": "0"})
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_security_headers_present_on_gateway_routes(client: TestClient) -> None:
    resp = client.get("/v1/gateway/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_gateway_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_GATEWAY", "0")
    c = TestClient(create_app(auth_required=False))
    assert c.get("/v1/gateway/health").status_code == 404   # not mounted
    assert c.get("/v1/health").status_code == 200            # legacy still up
