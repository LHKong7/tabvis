"""Phase 5 — full /v1+WS API, Agent Context & intent convergence (ROADMAP.md).

Covers INT-6 (ExecutionRegistry + router records + cancel/retry), INT-5 (low-level tools record an
execution when the intent surface is on), RT-3 (registration + credential + Agent Context), RT-2
(WebSocket /v1/events connects), and the RT-4 delegating shells. ``config_home`` (autouse) roots
state in a tmp dir.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import tabvis.browser.intents.execution_registry as exreg
from tabvis.agent.agents import credentials, registry as r
from tabvis.browser.intents import (
    Intent,
    get_execution_registry,
    get_intent_router,
    is_retryable,
)
from tabvis.browser.intents.types import ExecutionRecord
from tabvis.tool import ToolUseContext
from tabvis.agent.tools.browser_common import sync_browser_session


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _reset()
    yield
    _reset()


def _reset() -> None:
    exreg._registry = None
    credentials._by_token.clear()
    credentials._loaded = False
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False


# --------------------------------------------------------------------------- INT-6: ExecutionRegistry


def test_execution_registry_record_get_cancel() -> None:
    reg = get_execution_registry()
    rec = ExecutionRecord(execution_id="exec_t", intent="navigate", status="running")
    reg.record(rec)
    assert reg.get("exec_t") is rec
    assert reg.list_recent(1)[0] is rec
    assert reg.cancel("exec_t") is True
    assert reg.get("exec_t").status == "cancelled"
    assert reg.cancel("exec_t") is False           # already terminal
    assert is_retryable("snapshot") and not is_retryable("navigate")


def test_router_records_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    rec = asyncio.run(
        get_intent_router().route(Intent("navigate", {"url": "https://evil.test"}), agent_id="ag_x")
    )
    assert rec.status == "blocked"
    assert get_execution_registry().get(rec.execution_id) is rec   # router registered it


# --------------------------------------------------------------------------- INT-5: low-level convergence


def test_int5_low_level_action_records_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_INTENTS", "1")
    reg = get_execution_registry()
    before = len(reg.list_recent())
    asyncio.run(
        sync_browser_session(
            ToolUseContext(),
            {"url": "https://a.com", "title": "A"},
            event={"type": "navigation", "action": "goto", "url": "https://a.com"},
        )
    )
    after = reg.list_recent()
    assert len(after) == before + 1 and after[0].intent == "goto"

    # An event carrying an _execution_id (the intent tool's already-recorded path) does NOT duplicate.
    n = len(after)
    asyncio.run(
        sync_browser_session(
            ToolUseContext(),
            {"url": "https://a.com"},
            event={"type": "page", "action": "navigate", "_execution_id": "exec_ext"},
        )
    )
    assert len(reg.list_recent()) == n


def test_int5_disabled_records_nothing() -> None:
    reg = get_execution_registry()
    before = len(reg.list_recent())
    asyncio.run(
        sync_browser_session(
            ToolUseContext(), {"url": "https://a.com"},
            event={"type": "navigation", "action": "goto", "url": "https://a.com"},
        )
    )
    assert len(reg.list_recent()) == before   # intents flag off → no execution recorded


# --------------------------------------------------------------------------- RT-3: credentials


def test_register_creates_agent_and_credential() -> None:
    result = credentials.register(cwd="/repo", model="m")
    assert result["agent_id"].startswith("ag_")
    assert result["credential"].startswith("cred_")
    assert r.get(result["agent_id"]) is not None                       # agent record created
    assert credentials.resolve_agent_id(result["credential"]) == result["agent_id"]
    assert credentials.resolve_agent_id("cred_bogus") is None


# --------------------------------------------------------------------------- RT-2/RT-3/RT-4: server


def _client() -> Any:
    from starlette.testclient import TestClient

    from tabvis.browser.server import create_app

    return TestClient(create_app())


def test_register_endpoint_and_credential_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BASE_URL", "x")
    monkeypatch.setenv("TABVIS_API_KEY", "y")
    client = _client()
    reg = client.post("/v1/agents/register", json={}).json()
    assert reg["agent_id"] and reg["credential"]

    # A body agent_id that disagrees with the credential is rejected (403), before any run starts.
    resp = client.post(
        "/agent",
        headers={"X-Tabvis-Agent-Credential": reg["credential"]},
        json={"prompt": "hi", "agent_id": "ag_someone_else"},
    )
    assert resp.status_code == 403


def test_execution_and_identity_shells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BASE_URL", "x")
    monkeypatch.setenv("TABVIS_API_KEY", "y")
    client = _client()
    assert client.get("/v1/executions/exec_none").status_code == 404
    assert client.post("/v1/executions/exec_none/cancel").json()["cancelled"] is False
    assert client.get("/v1/agents/ag_none/identity").status_code == 404
    assert client.post("/v1/workspaces/ws_none/intents", json={"intent": "navigate"}).status_code == 404


def test_ws_events_endpoint_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BASE_URL", "x")
    monkeypatch.setenv("TABVIS_API_KEY", "y")
    client = _client()
    # The WebSocket channel accepts a connection (RT-2). With the bus off there are simply no frames.
    with client.websocket_connect("/v1/events") as ws:
        ws.close()
