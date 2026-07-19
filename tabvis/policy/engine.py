"""The policy engine core (PP-1).

``docs/permission-policy-engine_v1.md`` §3.2 & §6. Given ``(action, resource, mode)`` the engine
produces an ``allow`` / ``deny`` / ``ask`` decision:

1. **any** matching ``deny`` → deny   (absolute — a deny cannot be granted away at the engine level)
2. else the **last** matching non-deny rule wins — i.e. the highest-priority source, because sources
   are merged low→high (baseline → settings → identity → grant) into one ordered ruleset at
   construction (§3.1). This is what lets a scoped grant (``allow``) upgrade a baseline ``ask``.
3. no rule matched → the mode's fallback effect

Deny-terminal-plus-last-wins keeps the two design constraints consistent: deny-overrides is absolute
(a config-protection deny is never grantable), while ``ask`` vs ``allow`` is decided by source
priority, not a fixed effect ranking. The engine is a pure function of its compiled ruleset — it reads
no files, opens no sockets, and holds no global state. Converting the engine :class:`PolicyDecision`
into the tool-facing ``tabvis.types.permissions.PermissionDecision`` is an adapter concern (PP-3+).
"""

from __future__ import annotations

from dataclasses import dataclass

from tabvis.policy.audit import PolicyDecisionEvent
from tabvis.policy.modes import (
    Mode,
    baseline_rules_for_mode,
    fallback_for_mode,
)
from tabvis.policy.resources import normalize_resource
from tabvis.policy.rules import Effect, PolicyRule


@dataclass(frozen=True)
class PolicyDecision:
    """The engine's decision for one ``(action, resource)`` under a mode."""

    effect: Effect
    action: str
    resource: str
    mode: str
    matched_rule_id: str | None
    reason: str

    def to_audit(self, **correlation: str | None) -> PolicyDecisionEvent:
        """Build the :class:`PolicyDecisionEvent` audit record, threading in correlation ids."""
        return PolicyDecisionEvent(
            effect=self.effect,
            action=self.action,
            resource=self.resource,
            mode=self.mode,
            rule_id=self.matched_rule_id,
            reason=self.reason,
            **{k: v for k, v in correlation.items() if v is not None},
        )


class PolicyEngine:
    """Evaluates ``(action, resource)`` against an ordered ruleset with a fallback effect."""

    def __init__(self, rules: list[PolicyRule], fallback: Effect, mode: str = "custom") -> None:
        self._rules = list(rules)
        self._fallback = fallback
        self._mode = mode

    @classmethod
    def for_mode(cls, mode: Mode, extra_rules: list[PolicyRule] | None = None) -> "PolicyEngine":
        """Build an engine from a mode's baseline, with higher-priority ``extra_rules`` layered on top.

        ``extra_rules`` (settings.json / identity / grants, already ordered by priority) come *after*
        the baseline. Order does not change the deny>ask>allow verdict — precedence is by effect, not
        position — but it keeps the ruleset a faithful record of the merge in §3.1.
        """
        rules = baseline_rules_for_mode(mode)
        if extra_rules:
            rules.extend(extra_rules)
        return cls(rules=rules, fallback=fallback_for_mode(mode), mode=mode)

    @property
    def rules(self) -> tuple[PolicyRule, ...]:
        return tuple(self._rules)

    def evaluate(self, action: str, resource: str) -> PolicyDecision:
        """Decide ``(action, resource)``: any deny wins; else the last (highest-priority) match."""
        ref = normalize_resource(resource)
        winner: PolicyRule | None = None
        for rule in self._rules:
            if not rule.matches(action, ref):
                continue
            if rule.effect == "deny":
                winner = rule
                break  # deny is absolute — scan no further, nothing can override it
            winner = rule  # last non-deny match wins (highest-priority source, merged low→high)

        if winner is not None:
            reason = f"matched rule {winner.id!r} ({winner.effect})"
            return PolicyDecision(
                effect=winner.effect,
                action=action,
                resource=resource,
                mode=self._mode,
                matched_rule_id=winner.id,
                reason=reason,
            )
        return PolicyDecision(
            effect=self._fallback,
            action=action,
            resource=resource,
            mode=self._mode,
            matched_rule_id=None,
            reason=f"no rule matched; mode {self._mode!r} fallback ({self._fallback})",
        )
