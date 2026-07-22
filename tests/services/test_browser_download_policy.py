"""Integration coverage for the real BrowserDownload → policy_guard chain (issue #2).

The prior tests only exercised the low-level ``PolicyEngine`` on ``browser.download`` directly; they
never went through ``_action_and_resource``, so the mapping bug (the download tool falling through to
the always-allowed ``browser.interact`` catch-all) slipped past. These drive the actual tool's
``check_permissions`` and the guard's ``evaluate`` so the mapping is covered end to end.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.agent.tools.browser_download_tool import browser_download_tool
from tabvis.browser import identity_store, policy_guard
from tabvis.constants.tools import BROWSER_DOWNLOAD_TOOL_NAME
from tabvis.policy import grants
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean() -> Any:
    identity_store._cache.clear()
    grants.clear()
    reset_settings_cache()
    yield
    identity_store._cache.clear()
    grants.clear()
    reset_settings_cache()


def _ctx(agent_id: str | None) -> Any:
    return SimpleNamespace(agent_id=agent_id, tool_use_id="tu_1")


def _check(url: str, ctx: Any) -> dict[str, Any]:
    return asyncio.run(browser_download_tool.check_permissions({"url": url}, ctx))


# --------------------------------------------------------------------------- action mapping


def test_download_maps_to_browser_download_action() -> None:
    action, resource = policy_guard._action_and_resource(
        BROWSER_DOWNLOAD_TOOL_NAME, {"url": "https://x.test/f.zip"}
    )
    assert action == "browser.download"
    assert resource == "url:https://x.test/f.zip"


# --------------------------------------------------------------------------- mode postures


def test_standard_mode_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "standard")
    d = _check("https://x.test/file.zip", _ctx("ag_std"))
    assert d["behavior"] == "ask"
    assert d["suggestions"][0]["type"] == "addRules"


def test_trusted_mode_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")
    assert _check("https://x.test/file.zip", _ctx("ag_tr"))["behavior"] == "allow"


def test_locked_mode_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    d = _check("https://x.test/file.zip", _ctx("ag_lock"))
    assert d["behavior"] == "deny"


def test_denied_origins_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")  # even under trusted, a deny is absolute
    ident = identity_store.resolve("ag_deny")
    ident.permissions.denied_origins = ["blocked.test"]
    d = _check("https://blocked.test/secret.zip", _ctx("ag_deny"))
    assert d["behavior"] == "deny"
    # a different host is still fine under trusted
    assert _check("https://ok.test/f.zip", _ctx("ag_deny"))["behavior"] == "allow"


def test_session_grant_upgrades_ask_to_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "standard")
    ctx = _ctx("ag_grant")
    assert _check("https://ok.test/file.zip", ctx)["behavior"] == "ask"
    # Approving the ask records a grant over the widened host scope, upgrading later downloads.
    grants.record_grant_from_ask(
        "browser.download", "url:https://ok.test/file.zip", agent_id="ag_grant"
    )
    assert _check("https://ok.test/other.zip", ctx)["behavior"] == "allow"
    # ...but only for the granted host.
    assert _check("https://elsewhere.test/f.zip", ctx)["behavior"] == "ask"


# --------------------------------------------------------------------------- non-tool evaluator


def test_evaluate_download_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "standard")
    effect, _ = policy_guard.evaluate_download("https://x.test/f.zip", "ag_h")
    assert effect == "ask"
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "trusted")
    effect, _ = policy_guard.evaluate_download("https://x.test/f.zip", "ag_h")
    assert effect == "allow"
