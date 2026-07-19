"""Phase 4 — first-class Workspace, Policy Guard & identity depth (ROADMAP.md).

Covers IDP-8 (PolicyGuard behind the browser tools), IDP-4 (IdentityBinding acquire/refresh/release),
IDP-5 (per-identity launch overlay), WS-3 (first-class Pages), WS-5 (Goal/Timeline), and WS-6
(WorkspaceManager + pause/close). No real browser is launched. ``config_home`` (autouse) roots
everything in a tmp dir.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tabvis.agent.agents import registry as r
from tabvis.browser import identity_store, manager as mgr, policy_guard, workspace as ws
from tabvis.browser.persistence import db
from tabvis.tool import ToolUseContext


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _reset()
    yield
    _reset()


def _reset() -> None:
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False
    identity_store._cache.clear()
    identity_store._bindings.clear()
    identity_store._binding_by_agent.clear()
    ws._by_id.clear()
    ws._id_by_agent.clear()
    mgr._workspaces.clear()
    mgr._slots.clear()
    db.close()


# --------------------------------------------------------------------------- IDP-8: Policy Guard


def test_policy_guard_allows_non_navigation_tools() -> None:
    assert policy_guard.evaluate("BrowserClick", {"ref": "e1"}, None)["behavior"] == "allow"
    assert policy_guard.evaluate("BrowserType", {"ref": "e1", "text": "x"}, None)["behavior"] == "allow"
    assert policy_guard.evaluate("BrowserSnapshot", {}, None)["behavior"] == "allow"


def test_policy_guard_navigation_uses_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # No allowlist → allow all.
    assert policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://a.com"}, None)["behavior"] == "allow"
    # Allowlist that excludes the target → not "allow" (ask, which headless resolves to deny).
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    assert policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.test"}, None)["behavior"] != "allow"


def test_browser_tools_route_through_policy_guard() -> None:
    from tabvis.agent.tools.browser_click_tool import browser_click_tool
    from tabvis.agent.tools.browser_snapshot_tool import browser_snapshot_tool

    ctx = ToolUseContext()
    assert asyncio.run(browser_click_tool.check_permissions({"ref": "e1"}, ctx))["behavior"] == "allow"
    assert asyncio.run(browser_snapshot_tool.check_permissions({}, ctx))["behavior"] == "allow"


# --------------------------------------------------------------------------- IDP-4: IdentityBinding


def test_identity_binding_acquire_and_release() -> None:
    binding = identity_store.acquire("ag_bind", workspace_id="ws_1")
    assert binding.binding_id.startswith("bnd_")
    assert binding.workspace_id == "ws_1"
    assert identity_store.get_by_agent("ag_bind").status == "in_use"       # acquire flips to in_use
    assert identity_store.get_binding_for_agent("ag_bind").binding_id == binding.binding_id

    refreshed = identity_store.refresh(binding.binding_id, expires_at="2030-01-01T00:00:00Z")
    assert refreshed is not None and refreshed.expires_at == "2030-01-01T00:00:00Z"

    identity_store.release(binding.binding_id)
    assert identity_store.get_by_agent("ag_bind").status == "ready"        # release flips back
    assert identity_store.get_binding_for_agent("ag_bind") is None


def test_release_for_agent_is_a_noop_without_binding() -> None:
    identity_store.release_for_agent("ag_none")  # must not raise


# --------------------------------------------------------------------------- IDP-5: launch overlay


def test_launch_overlay_empty_for_fresh_identity() -> None:
    identity_store.resolve("ag_ov", profile_ref="/tmp/p")
    assert identity_store.launch_overlay("ag_ov") == {}   # nothing set → no launch change
    assert identity_store.launch_overlay(None) == {}


def test_launch_overlay_maps_set_fields_to_playwright_names() -> None:
    identity_store.resolve("ag_ov2", profile_ref="/tmp/p")
    identity_store.update_for_agent(
        "ag_ov2",
        {
            "environment": {"locale": "en-GB", "timezone": "Europe/London", "user_agent": "UA/1"},
            "network": {"proxy_ref": "http://proxy.local:8080"},
        },
    )
    overlay = identity_store.launch_overlay("ag_ov2")
    assert overlay == {
        "locale": "en-GB",
        "timezone_id": "Europe/London",   # design 'timezone' → Playwright 'timezone_id'
        "user_agent": "UA/1",
        "proxy": "http://proxy.local:8080",
    }


# --------------------------------------------------------------------------- WS-3 / WS-5 / WS-6


def test_page_id_is_stable_by_url() -> None:
    assert ws._page_id("https://a.com", 0) == ws._page_id("https://a.com", 5)   # url → stable id
    assert ws._page_id("https://a.com", 0) != ws._page_id("https://b.com", 0)
    assert ws._page_id(None, 3) == "pg_3"                                        # positional fallback


def test_workspace_goal_and_snapshot_shape() -> None:
    rec = ws.register_workspace(agent_id="ag_ws5", user_data_dir="/tmp/p", session_id="s")
    ws.set_goal(rec.workspace_id, "research browseros")
    snap = ws.snapshot(rec.workspace_id)
    assert snap["goal"] == "research browseros"
    assert snap["status"] == "active"
    assert isinstance(snap["pages"], list)       # WS-3 first-class page list
    assert isinstance(snap["timeline"], list)    # WS-5 timeline (empty with the bus off)


def test_workspace_manager_pause_and_list() -> None:
    manager = ws.get_workspace_manager()
    rec = manager.create(agent_id="ag_wm", user_data_dir="/tmp/p", session_id="s")
    assert manager.pause(rec.workspace_id).status == "paused"
    assert manager.snapshot(rec.workspace_id)["status"] == "paused"
    assert manager.resume(rec.workspace_id).status == "active"
    listing = manager.list()
    assert any(s["workspace_id"] == rec.workspace_id for s in listing)


def test_workspace_close_marks_closed() -> None:
    rec = ws.register_workspace(agent_id="ag_close", user_data_dir="/tmp/p", session_id="s")
    closed = asyncio.run(ws.close_workspace(rec.workspace_id))
    assert closed is False                      # nothing live was open
    assert ws.snapshot(rec.workspace_id)["status"] == "closed"


# --------------------------------------------------------------------------- spawn wiring (IDP-4 at spawn)


def test_spawn_acquires_identity_binding() -> None:
    mgr.init_browser_session(session_id="s1", model="m", cwd="/tmp", agent_id="ag_spawn4", profile="p4")
    assert identity_store.get_by_agent("ag_spawn4").status == "in_use"        # acquired at spawn
    binding = identity_store.get_binding_for_agent("ag_spawn4")
    assert binding is not None and binding.workspace_id == ws.get_workspace_for_agent("ag_spawn4").workspace_id

    # closing the browser releases the binding and flips the identity back to ready.
    asyncio.run(mgr.close_browser(ws.get_workspace_for_agent("ag_spawn4").identity_ref))
    assert identity_store.get_by_agent("ag_spawn4").status == "ready"
