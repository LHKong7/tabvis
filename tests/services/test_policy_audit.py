"""PP-6 — policy.decision audit emission (docs/permission-policy-engine_v1.md §10).

Every allow/deny/ask must be recordable via correlation ids, with no secret in the record. Tests use
a captured sink to assert what the Browser adapter emits, including the shadow ``wouldBe`` tag and URL
query redaction.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tabvis.browser import identity_store, policy_guard
from tabvis.policy import audit as policy_audit
from tabvis.policy import grants
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("TABVIS_PERMISSION_MODE", raising=False)
    monkeypatch.delenv("TABVIS_PERMISSION_SHADOW", raising=False)
    monkeypatch.delenv("TABVIS_PERMISSION_AUDIT", raising=False)
    grants.clear()
    identity_store._cache.clear()
    reset_settings_cache()
    yield
    grants.clear()
    identity_store._cache.clear()
    reset_settings_cache()


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Register a capturing sink for the duration of a test."""
    records: list[dict[str, Any]] = []
    unsub = policy_audit.register_sink(records.append)
    yield records
    unsub()


def _ctx(agent_id: str | None, tool_use_id: str | None = "tu_1") -> Any:
    return SimpleNamespace(agent_id=agent_id, tool_use_id=tool_use_id)


# --------------------------------------------------------------------------- emission basics


def test_allow_is_audited_with_correlation_ids(captured: list[dict[str, Any]]) -> None:
    policy_guard.evaluate("BrowserClick", {"ref": "e1"}, _ctx("agA", "tu_42"))
    assert len(captured) == 1
    rec = captured[0]
    assert rec["event"] == "policy.decision" and rec["effect"] == "allow"
    assert rec["action"] == "browser.interact"
    assert rec["request_id"] == "tu_42" and rec["agent_id"] == "agA"


def test_deny_is_audited_with_rule(captured: list[dict[str, Any]]) -> None:
    ident = identity_store.resolve("agB")
    ident.permissions.denied_origins = ["evil.com"]
    policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agB"))
    rec = captured[-1]
    assert rec["effect"] == "deny" and rec["rule_id"] == "identity-deny-origin-0"


def test_navigation_allowlist_ask_is_audited(captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.test/p"}, _ctx(None))
    rec = captured[-1]
    assert rec["effect"] == "ask" and rec["rule_id"] == "navigation-allowlist"


# --------------------------------------------------------------------------- redaction


def test_url_query_and_fragment_redacted(captured: list[dict[str, Any]]) -> None:
    policy_guard.evaluate(
        "BrowserNavigate",
        {"action": "goto", "url": "https://ok.test/cb?token=secret123&x=1#frag"},
        _ctx(None),
    )
    rec = captured[-1]
    assert "token" not in rec["resource"] and "secret123" not in rec["resource"]
    assert rec["resource"] == "url:https://ok.test/cb"


# --------------------------------------------------------------------------- shadow + toggle


def test_shadow_records_wouldbe(captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    ident = identity_store.resolve("agC")
    ident.permissions.denied_origins = ["evil.com"]
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    served = policy_guard.evaluate("BrowserNavigate", {"action": "goto", "url": "https://evil.com/x"}, _ctx("agC"))
    assert served["behavior"] == "allow"  # shadow served allow
    rec = captured[-1]
    assert rec["effect"] == "deny"  # audit records the real (intended) effect
    assert "shadow" in rec["reason"].lower()


def test_audit_can_be_disabled(captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_AUDIT", "off")
    policy_guard.evaluate("BrowserClick", {"ref": "e1"}, _ctx("agD"))
    assert captured == []
