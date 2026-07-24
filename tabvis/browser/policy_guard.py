"""Policy Guard — the Browser adapter over the unified permission policy engine (IDP-8 / PP-3).

``design.md`` §"Policy Guard" + ``docs/permission-policy-engine_v1.md`` §7. This is the single
permission entry for the Browser* tools: every tool's ``check_permissions`` calls :func:`evaluate`.
Historically it was a thin shell (navigation allowlist; everything else allowed). PP-3 rewires it as
an *adapter* — it maps a tool call to a ``(action, resource)`` pair, evaluates it against a
:class:`tabvis.policy.PolicyEngine`, and converts the engine decision back into a
``PermissionDecision``. The guard no longer decides rules itself; the engine does.

Behavior is deliberately preserved (``standard`` posture unchanged):

* Browser navigation and interaction tools map to ``browser.navigate`` or
  ``browser.interact``, both of which a **browser baseline** allows — so with no identity or
  settings policy, the decision is ``allow`` exactly as before.
* Navigation ``goto`` still runs :func:`check_navigation_permission` first, preserving the domain
  allowlist and its ``addRules`` ``ask`` suggestion (and the headless ask→deny posture).
* Per-identity ``denied_origins`` compile to ``deny`` rules (deny is absolute); ``settings.json``
  ``permissions.rules`` layer in via the engine. A future download/upload/credential tool falls to
  the standard baseline's ``ask`` — the engine, not this file, enforces that.

Rule ordering (low→high priority): mode baseline → browser baseline → settings rules → identity
rules. ``deny`` wins regardless of position; ``ask``/``allow`` are resolved by the last (highest)
match, so an operator can downgrade an interaction to ``ask`` and a grant can upgrade it.

Origin-level identity denial currently gates *navigation* (whose target origin is in the tool input);
gating interaction on an already-open denied page needs the live page URL and is a later refinement.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tabvis.constants.tools import (
    BROWSER_CLICK_TOOL_NAME,
    BROWSER_DOWNLOAD_TOOL_NAME,
    BROWSER_KEYS_TOOL_NAME,
    BROWSER_NAVIGATE_TOOL_NAME,
    BROWSER_SCROLL_TOOL_NAME,
    BROWSER_SNAPSHOT_TOOL_NAME,
    BROWSER_TYPE_TOOL_NAME,
    BROWSER_WAIT_TOOL_NAME,
)
from tabvis.policy.engine import PolicyEngine
from tabvis.policy.modes import baseline_rules_for_mode, fallback_for_mode
from tabvis.policy.rules import PolicyRule, compile_rules
from tabvis.policy import audit as policy_audit
from tabvis.policy import grants as grant_store
from tabvis.policy.audit import PolicyDecisionEvent
from tabvis.policy.settings_source import (
    is_shadow_mode,
    load_policy_rules_from_settings,
    resolve_mode,
)
from tabvis.types.permissions import PermissionDecision

# Browser baseline: today's permissive posture for the two action classes the current tools use.
# Layered *above* the mode baseline but *below* settings/identity, so operator rules can still
# override it and a deny anywhere still wins.
_BROWSER_BASELINE_RAW = [
    {"id": "browser-navigate-default", "effect": "allow", "actions": ["browser.navigate"], "resources": ["**"]},
    {"id": "browser-interact-default", "effect": "allow", "actions": ["browser.interact"], "resources": ["**"]},
]


def _host_to_url_patterns(entry: str) -> list[str]:
    """Turn a ``denied_origins`` entry (host, ``*.host``, or origin) into ``url:`` resource patterns.

    Mirrors the allowlist's host semantics: an apex host and its subdomains via ``*.host``. Both the
    bare-origin and with-path forms are emitted so ``https://evil.com`` and ``https://evil.com/x``
    both match.
    """
    e = (entry or "").strip().lower()
    if not e:
        return []
    if "://" in e:
        base = e.rstrip("/")
        return [f"url:{base}", f"url:{base}/**"]
    return [f"url:*://{e}", f"url:*://{e}/**"]


def _identity_rules(context: Any) -> list[PolicyRule]:
    """Compile per-identity ``denied_origins`` into absolute ``deny`` rules for browser actions."""
    agent_id = getattr(context, "agent_id", None) if context is not None else None
    if not agent_id:
        return []
    try:
        from tabvis.browser import identity_store

        identity = identity_store.get_by_agent(agent_id)
    except Exception:  # noqa: BLE001 - identity lookup is best-effort; absence means no extra rules
        identity = None
    if identity is None:
        return []

    raw: list[dict[str, Any]] = []
    for i, entry in enumerate(identity.permissions.denied_origins):
        resources = _host_to_url_patterns(entry)
        if not resources:
            continue
        raw.append(
            {
                "id": f"identity-deny-origin-{i}",
                "effect": "deny",
                "actions": ["browser.navigate", "browser.interact", "browser.download"],
                "resources": resources,
            }
        )
    return compile_rules(raw)


def _browser_engine(context: Any) -> PolicyEngine:
    """Assemble the browser engine: mode baseline → browser baseline → settings → identity → grants."""
    mode = resolve_mode()
    agent_id = getattr(context, "agent_id", None) if context is not None else None
    rules: list[PolicyRule] = baseline_rules_for_mode(mode)
    rules += compile_rules(_BROWSER_BASELINE_RAW)
    rules += load_policy_rules_from_settings()
    rules += _identity_rules(context)
    rules += grant_store.active_rules(agent_id)  # highest priority — upgrades ask, never deny
    return PolicyEngine(rules, fallback_for_mode(mode), mode=mode)


def _action_and_resource(tool_name: str, input: Any) -> tuple[str, str]:
    """Map a browser tool call to a ``(action, resource)`` pair for the engine."""
    from tabvis.agent.tools.browser_common import get_field

    if tool_name == BROWSER_NAVIGATE_TOOL_NAME:
        action = get_field(input, "action") or "goto"
        if action == "goto":
            return "browser.navigate", f"url:{get_field(input, 'url') or ''}"
        # back / forward / reload act on already-visited pages — no target origin to gate.
        return "browser.navigate", "session:page"
    if tool_name == BROWSER_DOWNLOAD_TOOL_NAME:
        # An explicit file fetch is a ``browser.download`` — the standard baseline asks for it, an
        # identity ``denied_origins`` denies it, a grant can upgrade it. Without this branch the tool
        # fell through to the ``browser.interact`` catch-all and was silently always-allowed.
        return "browser.download", f"url:{get_field(input, 'url') or ''}"
    if tool_name in (
        BROWSER_CLICK_TOOL_NAME,
        BROWSER_KEYS_TOOL_NAME,
        BROWSER_SCROLL_TOOL_NAME,
        BROWSER_TYPE_TOOL_NAME,
        BROWSER_SNAPSHOT_TOOL_NAME,
        BROWSER_WAIT_TOOL_NAME,
    ):
        # Interaction/observation on the current page; its origin is not in the tool input (see
        # module docstring) so the resource is the generic page handle.
        return "browser.interact", "session:page"
    # Unknown browser tool: route as interaction so an explicit rule can still gate it.
    return "browser.interact", "session:page"


def _to_permission_decision(
    effect: str, rule_id: str | None, action: str, resource: str, tool_name: str, input: Any
) -> PermissionDecision:
    """Convert an engine effect into the tool-facing ``PermissionDecision``."""
    if effect == "allow":
        return {"behavior": "allow", "updatedInput": input}
    if effect == "deny":
        detail = f" by rule {rule_id!r}" if rule_id else ""
        # Deny is absolute — an "add allow rule" suggestion would be futile, so name the blocking rule
        # and say how to actually change it (edit that rule / the identity's denied_origins).
        return {
            "behavior": "deny",
            "message": (
                f"Tabvis blocked {action} on {resource}{detail} (permission policy). This is an "
                f"absolute deny; to allow it, remove or amend that rule."
            ),
            "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource},
        }
    # ask — actionable: a structured addRules suggestion, and a note that approving is remembered.
    return {
        "behavior": "ask",
        "message": (
            f"Tabvis wants to perform {action} on {resource}, which requires approval under the "
            f"current permission policy. Approving is remembered for this agent (scoped grant)."
        ),
        "updatedInput": input,
        "suggestions": [
            {
                "type": "addRules",
                "destination": "localSettings",
                "rules": [{"toolName": tool_name, "ruleContent": f"action:{action}"}],
                "behavior": "allow",
            }
        ],
        "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource},
    }


def _apply_shadow(decision: PermissionDecision, input: Any) -> PermissionDecision:
    """In shadow mode, never block: serve any non-allow as allow, tagging the intended effect.

    Records what *would* have happened in ``decisionReason`` so an audit (PP-6) can measure real
    ask/deny frequency before a policy is switched to enforcing.
    """
    if not is_shadow_mode() or decision.get("behavior") == "allow":
        return decision
    reason = dict(decision.get("decisionReason") or {})
    reason.update({"shadow": True, "wouldBe": decision.get("behavior")})
    return {"behavior": "allow", "updatedInput": input, "decisionReason": reason}


def _emit_audit(
    context: Any,
    action: str,
    resource: str,
    effect: str,
    rule_id: str | None,
    mode: str,
    served: PermissionDecision,
) -> None:
    """Emit the ``policy.decision`` audit event (PP-6), recording the served vs. intended effect."""
    shadowed = bool((served.get("decisionReason") or {}).get("shadow"))
    reason = f"matched rule {rule_id!r}" if rule_id else "no rule matched (mode fallback)"
    if shadowed:
        reason += f"; shadow mode served as allow (would be {effect})"
    policy_audit.emit(
        PolicyDecisionEvent(
            effect=effect,
            action=action,
            resource=resource,
            mode=mode,
            rule_id=rule_id,
            reason=reason,
            request_id=getattr(context, "tool_use_id", None) if context is not None else None,
            agent_id=getattr(context, "agent_id", None) if context is not None else None,
        )
    )


def evaluate(tool_name: str, input: Any, context: Any) -> PermissionDecision:
    """The Policy Guard decision for one browser tool call (PP-3 adapter over the engine).

    Navigation ``goto`` is gated first by the domain allowlist (unchanged — delegated to
    ``check_navigation_permission``, preserving its ``ask`` + ``addRules``). Whatever the allowlist
    allows is then re-checked against the engine so per-identity/settings ``deny`` still applies.
    Every other browser tool goes straight to the engine, where the browser baseline allows it unless
    a rule says otherwise.
    """
    action, resource = _action_and_resource(tool_name, input)

    # Authentication lock (design §4.2, §13.1): while a credential authentication holds the browser,
    # every ordinary Agent browser RPC is refused — no navigation, click, type, snapshot or download,
    # and no observation — so the Agent can neither interfere with nor watch the login. The dedicated
    # BrowserAuthenticate tool is exempt (it *is* the authentication).
    from tabvis.constants.tools import BROWSER_AUTHENTICATE_TOOL_NAME

    if tool_name != BROWSER_AUTHENTICATE_TOOL_NAME:
        from tabvis.browser import auth_lease

        session_id = getattr(context, "browser_session_id", None) if context is not None else None
        if auth_lease.is_authentication_locked(session_id) if session_id else auth_lease.any_authentication_locked():
            from tabvis.browser.host import BROWSER_AUTHENTICATION_LOCKED

            return {"behavior": "deny", "message": BROWSER_AUTHENTICATION_LOCKED}

    if tool_name == BROWSER_NAVIGATE_TOOL_NAME:
        from tabvis.agent.tools.browser_common import get_field

        if (get_field(input, "action") or "goto") == "goto":
            from tabvis.agent.tools.browser_common import check_navigation_permission

            base = check_navigation_permission(tool_name, input, context)
            if base.get("behavior") != "allow":
                # Allowlist says ask/deny — preserve it verbatim (addRules intact), but shadow mode
                # still lets it through (audit-only).
                served = _apply_shadow(base, input)
                _emit_audit(context, action, resource, base["behavior"], "navigation-allowlist",
                            resolve_mode(), served)
                return served

    decision = _browser_engine(context).evaluate(action, resource)
    result = _to_permission_decision(
        decision.effect, decision.matched_rule_id, action, resource, tool_name, input
    )
    served = _apply_shadow(result, input)
    _emit_audit(context, action, resource, decision.effect, decision.matched_rule_id, decision.mode, served)
    return served


def evaluate_download(url: str, agent_id: str | None) -> tuple[str, str | None]:
    """Evaluate ``browser.download`` for a URL against the engine → ``(effect, rule_id)``.

    A non-tool entry point for the download side of the browser service, where an *unexpected*
    download (a click that turned out to trigger one) must be judged without a ``ToolUseContext``.
    Returns the raw engine effect (``allow`` / ``ask`` / ``deny``) so the caller can decide whether
    to expose the file to the agent or quarantine it — there is no user to answer an ``ask`` at this
    point, so the caller treats anything other than ``allow`` as "not cleared".
    """
    context = SimpleNamespace(agent_id=agent_id)
    decision = _browser_engine(context).evaluate("browser.download", f"url:{url or ''}")
    _emit_audit(
        context,
        "browser.download",
        f"url:{url or ''}",
        decision.effect,
        decision.matched_rule_id,
        decision.mode,
        {"behavior": decision.effect},
    )
    return decision.effect, decision.matched_rule_id


class PolicyGuard:
    """Object form of :func:`evaluate`, for callers that prefer a guard instance."""

    @staticmethod
    def evaluate(tool_name: str, input: Any, context: Any) -> PermissionDecision:
        return evaluate(tool_name, input, context)
