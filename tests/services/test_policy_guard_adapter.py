"""PP-3 — Policy Guard as the Browser adapter over the unified engine.

Two things must hold: (1) behavior is preserved — with no identity/settings policy the five browser
tools still allow, and the navigation allowlist still asks; (2) the engine now genuinely enforces
per-identity ``denied_origins`` and routes future capability actions (download/upload/credential) to
the standard baseline's ``ask``. No real browser is launched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.browser import identity_store, policy_guard
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean() -> Any:
    identity_store._cache.clear()
    reset_settings_cache()
    yield
    identity_store._cache.clear()
    reset_settings_cache()


def _ctx(agent_id: str | None) -> Any:
    return SimpleNamespace(agent_id=agent_id)


# --------------------------------------------------------------------------- behavior preserved


@pytest.mark.parametrize("tool", ["BrowserClick", "BrowserType", "BrowserSnapshot", "BrowserWait"])
def test_non_navigation_tools_allow_by_default(tool: str) -> None:
    assert policy_guard.evaluate(tool, {"ref": "e1"}, None)["behavior"] == "allow"
    # ...even with an agent context that has no deny rules.
    identity_store.resolve("ag_clean")
    assert policy_guard.evaluate(tool, {"ref": "e1"}, _ctx("ag_clean"))["behavior"] == "allow"


def test_navigation_allowlist_still_asks(monkeypatch: pytest.MonkeyPatch) -> None:
    # Empty allowlist → allow all.
    assert policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://a.com"}, None)["behavior"] == "allow"
    # Configured allowlist excluding target → ask (headless resolves to deny), with addRules intact.
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.test"}, None)
    assert d["behavior"] == "ask"
    assert d["suggestions"][0]["type"] == "addRules"


# --------------------------------------------------------------------------- new enforcement


def test_identity_denied_origin_blocks_navigation() -> None:
    ident = identity_store.resolve("ag_denies")
    ident.permissions.denied_origins = ["evil.com", "*.tracker.test"]

    denied = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/login"}, _ctx("ag_denies"))
    assert denied["behavior"] == "deny"
    assert "policy" in denied["message"].lower()
    assert denied["decisionReason"]["rule"] == "identity-deny-origin-0"

    sub = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://ads.tracker.test/x"}, _ctx("ag_denies"))
    assert sub["behavior"] == "deny"


def test_identity_denied_origin_allows_other_hosts() -> None:
    ident = identity_store.resolve("ag_denies2")
    ident.permissions.denied_origins = ["evil.com"]
    ok = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://good.test/x"}, _ctx("ag_denies2"))
    assert ok["behavior"] == "allow"
    # apex-only deny must not catch a look-alike host
    apex = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://notevil.com/x"}, _ctx("ag_denies2"))
    assert apex["behavior"] == "allow"


def test_deny_is_absolute_even_with_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # Origin is on the allowlist but also identity-denied → deny wins (deny is absolute).
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "evil.com")
    ident = identity_store.resolve("ag_both")
    ident.permissions.denied_origins = ["evil.com"]
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("ag_both"))
    assert d["behavior"] == "deny"


def test_future_capability_action_falls_to_standard_ask() -> None:
    # A hypothetical download/upload/credential action is not in the browser baseline, so the
    # standard baseline's `std-external` ask governs it — the engine, not this adapter, enforces that.
    eng = policy_guard._browser_engine(None)
    assert eng.evaluate("browser.download", "url:https://x.test/file.zip").effect == "ask"
    assert eng.evaluate("credential.use", "secret:ref_1").effect == "ask"
    # while navigation/interaction stay allowed
    assert eng.evaluate("browser.navigate", "url:https://x.test/").effect == "allow"
    assert eng.evaluate("browser.interact", "session:page").effect == "allow"
