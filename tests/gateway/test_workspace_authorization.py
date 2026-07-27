"""The workspace/browser runtime routes must enforce ownership, like the /agents routes do.

``_guard_agent`` gated ``/agents/{id}/*`` but the whole first-class workspace and persistent-browser
surface went unguarded, so on an auth-required bind any agent credential could enumerate every
workspace and pause, close or drive another agent's browser. ``authorize_workspace`` and
``filter_visible_agents`` already existed for exactly this; the routes simply never called them.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tabvis.agent.agents import credentials
from tabvis.browser import workspace as ws_module
from tabvis.browser.server import create_app


@pytest.fixture
def two_agents(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TABVIS_BASE_URL", "http://model.invalid")
    victim = credentials.register(cwd="/tmp", model="m", profile="pv")
    attacker = credentials.register(cwd="/tmp", model="m", profile="pa")
    victim_ws = ws_module.register_workspace(
        agent_id=victim["agent_id"], user_data_dir="/tmp/pv"
    )
    attacker_ws = ws_module.register_workspace(
        agent_id=attacker["agent_id"], user_data_dir="/tmp/pa"
    )
    return attacker, victim_ws, attacker_ws


def _client() -> TestClient:
    return TestClient(create_app(auth_required=True))


def test_listing_shows_only_the_callers_own_workspaces(two_agents) -> None:
    attacker, victim_ws, attacker_ws = two_agents
    headers = {credentials.CREDENTIAL_HEADER: attacker["credential"]}
    with _client() as client:
        body = client.get("/v1/workspaces", headers=headers).json()
    ids = [w["workspace_id"] for w in body["workspaces"]]
    assert ids == [attacker_ws.workspace_id]
    assert victim_ws.workspace_id not in ids


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("GET", "/snapshot"),
        ("POST", "/pause"),
        ("POST", "/close"),
        ("POST", "/intents"),
    ],
)
def test_another_agents_workspace_is_forbidden(two_agents, method, suffix) -> None:
    attacker, victim_ws, _ = two_agents
    headers = {credentials.CREDENTIAL_HEADER: attacker["credential"]}
    with _client() as client:
        response = client.request(
            method,
            f"/v1/workspaces/{victim_ws.workspace_id}{suffix}",
            headers=headers,
            json={"intent": "navigate", "url": "http://example.invalid"},
        )
    assert response.status_code == 403


def test_the_owner_still_reaches_its_own_workspace(two_agents) -> None:
    attacker, _, attacker_ws = two_agents
    headers = {credentials.CREDENTIAL_HEADER: attacker["credential"]}
    with _client() as client:
        response = client.get(
            f"/v1/workspaces/{attacker_ws.workspace_id}/snapshot", headers=headers
        )
    assert response.status_code == 200


def test_an_unauthenticated_caller_is_rejected(two_agents) -> None:
    with _client() as client:
        assert client.get("/v1/workspaces").status_code == 401
        assert client.get("/v1/browsers").status_code == 401
