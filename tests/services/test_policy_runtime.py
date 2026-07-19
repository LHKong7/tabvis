"""PP-10 — Runtime API adapter + agent-to-agent isolation.

The ownership gate is the isolation core: a non-admin Principal may only touch resources it owns; a
cross-owner access is an absolute deny regardless of mode. Admins bypass ownership. The mode/settings
engine governs owner access (locked denies management by default). List visibility is enforced by
ownership, not query filtering.
"""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.policy import audit as policy_audit
from tabvis.policy import grants
from tabvis.policy.runtime_adapter import (
    Principal,
    RuntimeAccessDenied,
    authorize_agent,
    authorize_workspace,
    filter_visible_agents,
    require_agent_access,
)
from tabvis.utils.settings.settings import reset_settings_cache


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in ("TABVIS_PERMISSION_MODE", "TABVIS_PERMISSION_SHADOW"):
        monkeypatch.delenv(var, raising=False)
    grants.clear()
    reset_settings_cache()
    yield
    grants.clear()
    reset_settings_cache()


# --------------------------------------------------------------------------- agent isolation


def test_agent_can_read_own_record() -> None:
    p = Principal(agent_id="agA")
    assert authorize_agent(p, "runtime.read", "agA")["behavior"] == "allow"


def test_agent_cannot_read_another_agent() -> None:
    p = Principal(agent_id="agA")
    d = authorize_agent(p, "runtime.read", "agB")
    assert d["behavior"] == "deny"
    assert d["decisionReason"]["rule"] == "cross-owner-isolation"


def test_agent_cannot_cancel_another_agent() -> None:
    p = Principal(agent_id="agA")
    assert authorize_agent(p, "runtime.cancel", "agB")["behavior"] == "deny"


def test_admin_sees_any_agent() -> None:
    admin = Principal(is_admin=True)
    assert authorize_agent(admin, "runtime.read", "agB")["behavior"] == "allow"
    assert authorize_agent(admin, "runtime.cancel", "agB")["behavior"] == "allow"


def test_anonymous_principal_denied() -> None:
    p = Principal(agent_id=None)
    assert authorize_agent(p, "runtime.read", "agB")["behavior"] == "deny"


# --------------------------------------------------------------------------- workspace isolation


def test_workspace_owner_allowed_non_owner_denied() -> None:
    owner = Principal(agent_id="agA")
    other = Principal(agent_id="agC")
    assert authorize_workspace(owner, "runtime.read", "ws1", owner="agA")["behavior"] == "allow"
    assert authorize_workspace(other, "runtime.read", "ws1", owner="agA")["behavior"] == "deny"


def test_unowned_workspace_denied_for_non_admin() -> None:
    p = Principal(agent_id="agA")
    assert authorize_workspace(p, "runtime.read", "ws1", owner=None)["behavior"] == "deny"


# --------------------------------------------------------------------------- mode layering


def test_locked_denies_owner_management_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_MODE", "locked")
    p = Principal(agent_id="agA")
    # ownership passes (own record) but locked denies runtime.* by default
    assert authorize_agent(p, "runtime.manage", "agA")["behavior"] == "deny"
    # admin still allowed
    assert authorize_agent(Principal(is_admin=True), "runtime.manage", "agA")["behavior"] == "allow"


# --------------------------------------------------------------------------- guards + visibility


def test_require_agent_access_raises_on_cross_owner() -> None:
    p = Principal(agent_id="agA")
    require_agent_access(p, "runtime.read", "agA")  # no raise
    with pytest.raises(RuntimeAccessDenied):
        require_agent_access(p, "runtime.read", "agB")


def test_filter_visible_agents_enforces_ownership() -> None:
    p = Principal(agent_id="agA")
    # owner_of identity: an agent owns itself
    assert filter_visible_agents(p, ["agA", "agB", "agC"]) == ["agA"]
    # admin sees all
    assert filter_visible_agents(Principal(is_admin=True), ["agA", "agB"]) == ["agA", "agB"]
    # workspace-style ownership lookup
    owners = {"ws1": "agA", "ws2": "agB"}
    assert filter_visible_agents(p, ["ws1", "ws2"], owner_of=owners.get) == ["ws1"]


# --------------------------------------------------------------------------- audit + shadow


def test_cross_owner_deny_is_audited() -> None:
    records: list[dict[str, Any]] = []
    unsub = policy_audit.register_sink(records.append)
    try:
        authorize_agent(Principal(agent_id="agA"), "runtime.read", "agB")
    finally:
        unsub()
    assert records[-1]["effect"] == "deny" and records[-1]["rule_id"] == "cross-owner-isolation"


def test_shadow_serves_cross_owner_as_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_PERMISSION_SHADOW", "1")
    d = authorize_agent(Principal(agent_id="agA"), "runtime.read", "agB")
    assert d["behavior"] == "allow" and d["decisionReason"]["wouldBe"] == "deny"
