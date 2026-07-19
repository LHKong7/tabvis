"""Phase 1 foundational seams (ROADMAP.md) — additive, zero-behavior-change stubs.

Covers the six seams: OBS-1 RuntimeEvent envelope, IDP-1 identity vocabulary, PERS-1 persistence
facade + data root, WS-1 workspace record + snapshot, INT-1 intent/execution primitives + a
NavigateIntent handler, and RT-1 /v1 route aliases. None of these launch a real browser — the one
that touches navigation is exercised through its PolicyCheck (which runs before any launch).
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest


# --------------------------------------------------------------------------- OBS-1: RuntimeEvent


def test_obs1_runtime_event_envelope() -> None:
    from tabvis.browser.events import (
        EVENT_SOURCES,
        SCHEMA_VERSION,
        ObservationType,
        RawEventType,
        RuntimeEvent,
    )

    ev = RuntimeEvent(
        type=RawEventType.ACTION_PERFORMED,
        source="playwright",
        payload={"url": "https://example.com"},
        agent_id="ag_1",
        execution_id="exec_1",
    )
    assert ev.id.startswith("ev_")
    assert ev.timestamp  # ISO utc stamp
    assert ev.schema_version == SCHEMA_VERSION
    d = ev.to_dict()
    assert d["type"] == "action.performed"
    assert d["source"] == "playwright"
    assert d["agent_id"] == "ag_1"
    assert d["payload"]["url"] == "https://example.com"
    # The five design sources, and the semantic-observation vocabulary, are present.
    assert set(EVENT_SOURCES) == {"runtime", "playwright", "cdp", "extension", "filesystem"}
    assert ObservationType.ARTIFACT_DOWNLOADED == "artifact.downloaded"


def test_obs1_runtime_event_is_immutable() -> None:
    from tabvis.browser.events import RuntimeEvent

    ev = RuntimeEvent(type="page.loaded", source="runtime")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.type = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------------------- IDP-1: identity


def test_idp1_browser_identity_shape() -> None:
    from tabvis.browser.identity import BrowserIdentity, IdentityBinding

    ident = BrowserIdentity(agent_id="ag_42", name="research")
    assert ident.id.startswith("id_")
    assert ident.agent_id == "ag_42"
    assert ident.status == "ready"
    # All five design sub-objects exist and default empty.
    d = ident.to_dict()
    for key in ("profile", "auth", "network", "environment", "permissions"):
        assert key in d
    assert d["permissions"]["allowed_origins"] == []
    assert ident.metadata()["agent_id"] == "ag_42"

    binding = IdentityBinding(identity_id=ident.id, agent_id="ag_42", workspace_id="ws_1")
    assert binding.binding_id.startswith("bnd_")
    assert binding.identity_id == ident.id
    assert binding.capabilities == []


# --------------------------------------------------------------------------- PERS-1: persistence


def test_pers1_data_root_paths(config_home: str) -> None:
    from tabvis.browser import persistence as pers

    root = pers.get_browser_os_data_dir()
    assert root.startswith(config_home)
    assert root.endswith("browser-os-data")
    assert pers.runtime_db_path().endswith("runtime.db")
    assert pers.identities_dir("id_x").endswith("identities/id_x".replace("/", __import__("os").sep))
    # create=True actually makes the directory.
    made = pers.workspaces_dir("ws_x", create=True)
    assert __import__("os").path.isdir(made)


def test_pers1_service_singleton_and_delegation(config_home: str) -> None:
    from tabvis.agent.agents import registry
    from tabvis.browser.persistence import get_persistence_service

    svc = get_persistence_service()
    assert get_persistence_service() is svc  # process singleton

    # save_agent_record delegates to registry.persist → writes the same file at the same path.
    record = registry.AgentRecord(agent_id="ag_pers1", session_id="s1", prompt="hi")
    asyncio.run(svc.save_agent_record(record))
    assert __import__("os").path.exists(registry.record_path("ag_pers1"))


# --------------------------------------------------------------------------- WS-1: workspace


def test_ws1_register_is_idempotent_per_agent() -> None:
    from tabvis.browser import workspace as ws

    a = ws.register_workspace(agent_id="ag_ws1", user_data_dir="/tmp/p1", profile="default", session_id="s1")
    b = ws.register_workspace(agent_id="ag_ws1", user_data_dir="/tmp/p1", profile="default", session_id="s2")
    assert a.workspace_id.startswith("ws_")
    assert a.workspace_id == b.workspace_id       # same agent → same workspace id
    assert b.session_id == "s2"                   # session refreshes across re-runs
    assert ws.get_workspace_for_agent("ag_ws1").workspace_id == a.workspace_id
    assert ws.get_workspace(a.workspace_id) is a


def test_ws1_snapshot_shape() -> None:
    from tabvis.browser import workspace as ws

    rec = ws.register_workspace(agent_id="ag_ws2", user_data_dir="/tmp/p2", profile="default", session_id="s2")
    snap = ws.snapshot(rec.workspace_id)
    assert snap is not None
    # The six design fields (Agent ID / Goal / Task / Pages / Artifacts / Timeline) + ids.
    for key in ("workspace_id", "agent_id", "identity_ref", "goal", "task", "pages", "artifacts", "timeline"):
        assert key in snap
    assert snap["agent_id"] == "ag_ws2"
    assert snap["identity_ref"] == "/tmp/p2"      # today identity == profile dir
    assert snap["pages"] == []  # WS-3: first-class page list; empty with no live session record
    assert snap["artifacts"]["count"] == 0
    assert ws.snapshot("ws_does_not_exist") is None


# --------------------------------------------------------------------------- INT-1: intents


def test_int1_execution_engine_navigate_registered() -> None:
    from tabvis.browser.intents import get_execution_engine, new_execution_id

    assert new_execution_id().startswith("exec_")
    engine = get_execution_engine()
    assert engine.has("navigate")
    assert "navigate" in engine.handler_names()


def test_int1_unknown_intent_fails_cleanly() -> None:
    from tabvis.browser.intents import Intent, get_execution_engine

    rec = asyncio.run(get_execution_engine().run(Intent(name="nope")))
    assert rec.status == "failed"
    assert "no handler" in (rec.error or "")
    assert rec.execution_id.startswith("exec_")


def test_int1_navigate_missing_url_fails_without_launch() -> None:
    from tabvis.browser.intents import Intent, get_execution_engine

    rec = asyncio.run(get_execution_engine().run(Intent(name="navigate", params={})))
    assert rec.status == "failed"
    assert "url" in (rec.error or "")


def test_int1_navigate_policycheck_blocks_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.browser.intents import Intent, get_execution_engine

    # A non-empty allowlist that the target does NOT match → check_navigation_permission returns
    # "ask" (not "allow") → the handler blocks BEFORE get_or_create_browser_service is ever called.
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    rec = asyncio.run(
        get_execution_engine().run(
            Intent(name="navigate", params={"url": "https://evil.test/x"})
        )
    )
    assert rec.status == "blocked"
    assert rec.error


# --------------------------------------------------------------------------- RT-1: /v1 route aliases


def test_rt1_v1_aliases_and_workspace_route() -> None:
    from tabvis.browser.server import create_app

    app = create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/" in paths                                    # console, unversioned
    assert "/health" in paths and "/v1/health" in paths    # legacy + /v1 twin (incl. /v1/health)
    assert "/workspaces/{workspace_id}/snapshot" in paths
    assert "/v1/workspaces/{workspace_id}/snapshot" in paths

    # Every legacy API path has a /v1 twin (skip the console and the /ui static mount).
    legacy = {
        p
        for p in paths
        if p and p != "/" and not p.startswith("/v1") and not p.startswith("/ui")
    }
    for p in legacy:
        assert "/v1" + p in paths, f"missing /v1 alias for {p}"
