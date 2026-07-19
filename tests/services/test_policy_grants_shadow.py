"""PP-5 — scoped grants (de-noising), shadow mode, and actionable messages.

Covers the grant store (scope/TTL/widening, compile to allow rules) and its effect through the Browser
adapter: an approved ask stops re-asking within scope; a grant never overrides an absolute deny;
shadow mode serves non-allow as allow while recording the intended effect; deny/ask messages are
actionable. No real browser is launched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.browser import identity_store, policy_guard
from tabvis.policy import grants
from tabvis.policy.grants import grant_pattern_for_resource
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("TABVIS_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("TABVIS_PERMISSION_SHADOW", raising=False)
    grants.clear()
    identity_store._cache.clear()
    reset_settings_cache()
    yield
    grants.clear()
    identity_store._cache.clear()
    reset_settings_cache()


def _ctx(agent_id: str | None) -> Any:
    return SimpleNamespace(agent_id=agent_id)


# --------------------------------------------------------------------------- grant store units


def test_grant_pattern_widens_url_to_origin() -> None:
    assert grant_pattern_for_resource("url:https://ok.test/login?x=1") == "url:https://ok.test/**"
    assert grant_pattern_for_resource("url:HTTPS://OK.test:8443/a") == "url:https://ok.test:8443/**"
    # non-url resources stay exact
    assert grant_pattern_for_resource("session:page") == "session:page"


def test_active_grants_respect_scope_and_ttl() -> None:
    grants.add_grant("browser.download", "url:https://ok.test/**", agent_id="ag1", ttl_seconds=100, now=1000.0)
    # same agent, before expiry
    assert len(grants.active_grants("ag1", now=1050.0)) == 1
    # other agent → not in scope
    assert grants.active_grants("ag2", now=1050.0) == []
    # after expiry
    assert grants.active_grants("ag1", now=1200.0) == []


def test_global_grant_applies_to_any_agent() -> None:
    grants.add_grant("browser.download", "url:https://ok.test/**", agent_id=None, now=1000.0)
    assert len(grants.active_grants("anyone", now=1000.0)) == 1


def test_purge_and_revoke() -> None:
    g = grants.add_grant("network.request", "url:https://x/**", ttl_seconds=10, now=1000.0)
    assert grants.purge_expired(now=1020.0) == 1
    g2 = grants.add_grant("network.request", "url:https://y/**", now=1000.0)
    assert grants.revoke(g2.id) is True
    assert grants.revoke("nonexistent") is False


# --------------------------------------------------------------------------- de-noising via adapter


def test_grant_upgrades_a_baseline_ask() -> None:
    eng_before = policy_guard._browser_engine(_ctx("agD"))
    assert eng_before.evaluate("browser.download", "url:https://ok.test/file").effect == "ask"

    # Approve once → grant recorded over the widened origin.
    grants.record_grant_from_ask("browser.download", "url:https://ok.test/file", agent_id="agD")

    eng_after = policy_guard._browser_engine(_ctx("agD"))
    assert eng_after.evaluate("browser.download", "url:https://ok.test/other").effect == "allow"
    # a different host still asks
    assert eng_after.evaluate("browser.download", "url:https://elsewhere.test/f").effect == "ask"
    # a different agent still asks (scope)
    assert policy_guard._browser_engine(_ctx("agE")).evaluate("browser.download", "url:https://ok.test/x").effect == "ask"


def test_grant_cannot_override_absolute_deny() -> None:
    ident = identity_store.resolve("agF")
    ident.permissions.denied_origins = ["evil.com"]
    # Even with a grant for the exact origin, the identity deny wins.
    grants.add_grant("browser.navigate", "url:https://evil.com/**", agent_id="agF")
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agF"))
    assert d["behavior"] == "deny"


# --------------------------------------------------------------------------- shadow mode


def test_shadow_mode_serves_deny_as_allow_with_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    ident = identity_store.resolve("agG")
    ident.permissions.denied_origins = ["evil.com"]
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agG"))
    assert d["behavior"] == "allow"
    assert d["decisionReason"]["shadow"] is True
    assert d["decisionReason"]["wouldBe"] == "deny"


def test_shadow_mode_serves_allowlist_ask_as_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.test"}, _ctx(None))
    assert d["behavior"] == "allow"
    assert d["decisionReason"]["wouldBe"] == "ask"


def test_shadow_off_still_enforces() -> None:
    ident = identity_store.resolve("agH")
    ident.permissions.denied_origins = ["evil.com"]
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agH"))
    assert d["behavior"] == "deny"


# --------------------------------------------------------------------------- actionable messages


def test_deny_message_is_actionable() -> None:
    ident = identity_store.resolve("agI")
    ident.permissions.denied_origins = ["evil.com"]
    d = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agI"))
    assert "remove or amend" in d["message"].lower()
    assert d["decisionReason"]["rule"] == "identity-deny-origin-0"
