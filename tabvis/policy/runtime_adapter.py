"""Runtime API adapter + agent-to-agent isolation (PP-10).

``docs/permission-policy-engine_v1.md`` §7 (Runtime API) / §10: management, cancel, export, log and
artifact access all go through one Policy Engine, and — critically — **one agent must not read another
agent's workspace / artifacts / logs / record**. The Principal comes from the authenticated request
context, never from a body-supplied ``agent_id``.

Authorization is two-stage:

1. **Ownership gate** (the isolation core): a non-admin Principal may only touch a resource it owns —
   its own ``agent:<id>`` record, or a workspace whose ``owner_agent`` is itself. A cross-owner access
   is an absolute deny, regardless of mode. An ``is_admin`` Principal (the management console) bypasses
   the ownership gate but is still audited.
2. **Engine policy**: once ownership passes, the mode/settings engine decides — a runtime baseline
   allows ``runtime.*`` on owned agent/workspace resources in trusted/standard, but ``locked`` denies
   by default (management must be explicitly granted in a locked deployment).

The live API server does not yet authenticate a Principal (that wiring is P0-2); this module provides
the decision and the list-visibility filter those endpoints call once a Principal is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from tabvis.policy import audit as policy_audit
from tabvis.policy import grants as grant_store
from tabvis.policy.audit import PolicyDecisionEvent
from tabvis.policy.engine import PolicyEngine
from tabvis.policy.modes import baseline_rules_for_mode, fallback_for_mode
from tabvis.policy.rules import PolicyRule, compile_rules
from tabvis.policy.settings_source import (
    is_shadow_mode,
    load_policy_rules_from_settings,
    resolve_mode,
)
from tabvis.types.permissions import PermissionDecision


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: an agent-scoped principal, or the management console (``is_admin``)."""

    agent_id: str | None = None
    is_admin: bool = False


# Runtime baseline: owner access to runtime.* is allowed outside locked (ownership is gated
# separately, before the engine). In locked, management needs an explicit settings/grant allow.
_RUNTIME_BASELINE = [
    {
        "id": "runtime-owner-default",
        "effect": "allow",
        "actions": ["runtime.read", "runtime.manage", "runtime.cancel", "runtime.export"],
        "resources": ["agent:**", "workspace:**"],
    },
]


class RuntimeAccessDenied(PermissionError):
    """Raised by the ``require_*`` guards when a Principal is not allowed to touch a resource."""


def _runtime_engine(principal: Principal) -> PolicyEngine:
    mode = resolve_mode()
    rules: list[PolicyRule] = baseline_rules_for_mode(mode)
    if mode != "locked":
        rules += compile_rules(_RUNTIME_BASELINE)
    rules += load_policy_rules_from_settings()
    rules += grant_store.active_rules(principal.agent_id)
    return PolicyEngine(rules, fallback_for_mode(mode), mode=mode)


def _emit(principal: Principal, action: str, resource: str, effect: str, rule_id: str | None, mode: str, shadowed: bool) -> None:
    reason = f"matched rule {rule_id!r}" if rule_id else "no rule matched (mode fallback)"
    if shadowed:
        reason += f"; shadow mode served as allow (would be {effect})"
    policy_audit.emit(
        PolicyDecisionEvent(
            effect=effect, action=action, resource=resource, mode=mode, rule_id=rule_id, reason=reason,
            agent_id=principal.agent_id,
        )
    )


def _deny(action: str, resource: str, reason_rule: str) -> PermissionDecision:
    return {
        "behavior": "deny",
        "message": f"Principal is not allowed to {action} on {resource} ({reason_rule}).",
        "decisionReason": {"type": "rule", "rule": reason_rule, "action": action, "resource": resource},
    }


def authorize(principal: Principal, action: str, resource: str, owner: str | None) -> PermissionDecision:
    """Authorize ``principal`` to perform ``action`` on ``resource`` owned by ``owner``.

    Ownership gate first (cross-owner → absolute deny), then the mode/settings engine. Admins bypass
    ownership. Emits an audit event; honors shadow mode.
    """
    mode = resolve_mode()
    # 1. Ownership gate — the isolation core.
    if not principal.is_admin:
        if owner is None or principal.agent_id is None or principal.agent_id != owner:
            shadowed = is_shadow_mode()
            _emit(principal, action, resource, "deny", "cross-owner-isolation", mode, shadowed)
            if shadowed:
                return {"behavior": "allow", "decisionReason": {"shadow": True, "wouldBe": "deny", "rule": "cross-owner-isolation"}}
            return _deny(action, resource, "cross-owner-isolation")

    # 2. Engine policy (admin is allowed but still audited).
    if principal.is_admin:
        effect, rule_id = "allow", "admin"
    else:
        d = _runtime_engine(principal).evaluate(action, resource)
        effect, rule_id = d.effect, d.matched_rule_id

    shadowed = is_shadow_mode() and effect != "allow"
    _emit(principal, action, resource, effect, rule_id, mode, shadowed)
    if shadowed:
        return {"behavior": "allow", "decisionReason": {"shadow": True, "wouldBe": effect, "rule": rule_id}}
    if effect == "deny":
        return _deny(action, resource, rule_id or "mode")
    if effect == "ask":
        return {"behavior": "ask", "message": f"Approval required to {action} on {resource}.",
                "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource}}
    return {"behavior": "allow"}


def authorize_agent(principal: Principal, action: str, target_agent_id: str) -> PermissionDecision:
    """Authorize access to another agent's runtime resource (an agent owns itself)."""
    return authorize(principal, action, f"agent:{target_agent_id}", owner=target_agent_id)


def authorize_workspace(principal: Principal, action: str, workspace_id: str, owner: str | None) -> PermissionDecision:
    """Authorize access to a workspace whose ``owner_agent`` is ``owner`` (looked up by the caller)."""
    return authorize(principal, action, f"workspace:{workspace_id}", owner=owner)


def require_agent_access(principal: Principal, action: str, target_agent_id: str) -> None:
    """Guard form: raise :class:`RuntimeAccessDenied` unless the access is allowed."""
    d = authorize_agent(principal, action, target_agent_id)
    if d.get("behavior") != "allow":
        raise RuntimeAccessDenied(d.get("message", "access denied"))


def filter_visible_agents(
    principal: Principal,
    agent_ids: list[str],
    *,
    owner_of: Callable[[str], str | None] | None = None,
) -> list[str]:
    """The subset of ``agent_ids`` the Principal may see — enforced by ownership, not query filtering.

    ``owner_of`` maps an id to its owner (defaults to identity — an agent owns itself). Admins see all.
    This is what a list / SSE / WS endpoint uses so it never leaks another agent's runs.
    """
    if principal.is_admin:
        return list(agent_ids)
    resolve_owner = owner_of or (lambda a: a)
    return [a for a in agent_ids if principal.agent_id is not None and principal.agent_id == resolve_owner(a)]
