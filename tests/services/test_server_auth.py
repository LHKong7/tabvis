"""P0-2 — Runtime API authentication + agent isolation over HTTP.

Drives the real Starlette app. In the default (loopback / auth_required=False) posture the management
face is open, as before. With auth_required=True an unauthenticated caller is rejected and a per-agent
credential can only reach its own agent. Also covers the startup guard and transport headers.
"""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.browser import server_auth
from tabvis.agent.agents import credentials, registry as r


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("TABVIS_SERVER_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("TABVIS_SERVER_CORS_ORIGINS", raising=False)
    credentials._by_token.clear()
    credentials._loaded = False
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False
    yield
    credentials._by_token.clear()
    credentials._loaded = False
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False


def _client(auth_required: bool):
    from starlette.testclient import TestClient

    from tabvis.browser.server import create_app

    return TestClient(create_app(auth_required=auth_required))


def _register() -> tuple[str, str]:
    """Register an agent; returns (agent_id, credential)."""
    res = credentials.register(cwd="/repo", model="m")
    return res["agent_id"], res["credential"]


# --------------------------------------------------------------------------- host / startup guard


def test_is_loopback_classification() -> None:
    assert server_auth.is_loopback("127.0.0.1") and server_auth.is_loopback("localhost")
    assert not server_auth.is_loopback("0.0.0.0") and not server_auth.is_loopback("10.0.0.5")


def test_startup_refuses_public_bind_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_SERVER_ADMIN_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        server_auth.enforce_startup_auth("0.0.0.0")
    # with a token it is allowed
    monkeypatch.setenv("TABVIS_SERVER_ADMIN_TOKEN", "secret")
    server_auth.enforce_startup_auth("0.0.0.0")  # no raise
    # loopback never needs a token
    monkeypatch.delenv("TABVIS_SERVER_ADMIN_TOKEN", raising=False)
    server_auth.enforce_startup_auth("127.0.0.1")


# --------------------------------------------------------------------------- open (loopback) posture


def test_loopback_open_lists_agents() -> None:
    _register()
    c = _client(auth_required=False)
    assert c.get("/v1/agents").status_code == 200  # no auth needed on loopback


# --------------------------------------------------------------------------- enforced posture


def test_enforced_requires_auth() -> None:
    c = _client(auth_required=True)
    assert c.get("/v1/agents").status_code == 401
    aid, _ = _register()
    assert c.get(f"/v1/agents/{aid}").status_code == 401


def test_agent_can_reach_own_but_not_others() -> None:
    a_id, a_cred = _register()
    b_id, _ = _register()
    c = _client(auth_required=True)
    hdr = {"x-tabvis-agent-credential": a_cred}
    # own agent → allowed (200)
    assert c.get(f"/v1/agents/{a_id}", headers=hdr).status_code == 200
    # another agent → forbidden (403), and existence is not leaked
    assert c.get(f"/v1/agents/{b_id}", headers=hdr).status_code == 403
    assert c.post(f"/v1/agents/{b_id}/cancel", headers=hdr).status_code == 403
    assert c.get(f"/v1/agents/{b_id}/identity", headers=hdr).status_code == 403


def test_list_filtered_to_own_agents() -> None:
    a_id, a_cred = _register()
    _register()  # a second agent the caller does not own
    c = _client(auth_required=True)
    body = c.get("/v1/agents", headers={"x-tabvis-agent-credential": a_cred}).json()
    assert body["count"] == 1 and body["agents"][0]["agent_id"] == a_id


def test_admin_token_sees_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SERVER_ADMIN_TOKEN", "s3cret")
    _register()
    _register()
    c = _client(auth_required=True)
    body = c.get("/v1/agents", headers={"authorization": "Bearer s3cret"}).json()
    assert body["count"] == 2


# --------------------------------------------------------------------------- transport headers


def test_security_headers_present() -> None:
    c = _client(auth_required=False)
    resp = c.get("/v1/health")
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("x-frame-options") == "DENY"


def test_cors_default_deny_and_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client(auth_required=False)
    # default: no allow-origin header
    assert "access-control-allow-origin" not in c.get("/v1/health", headers={"origin": "https://evil.test"}).headers
    monkeypatch.setenv("TABVIS_SERVER_CORS_ORIGINS", "https://ok.test")
    c2 = _client(auth_required=False)
    r_ok = c2.get("/v1/health", headers={"origin": "https://ok.test"})
    assert r_ok.headers.get("access-control-allow-origin") == "https://ok.test"
    r_bad = c2.get("/v1/health", headers={"origin": "https://evil.test"})
    assert "access-control-allow-origin" not in r_bad.headers
