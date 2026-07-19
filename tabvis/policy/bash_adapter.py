"""Bash adapter over the unified permission policy engine (PP-8).

``docs/permission-policy-engine_v1.md`` §7 (Bash). Layers a ``shell.execute`` (and best-effort
``network.request``) policy decision on top of the existing rich bash permission resolver, taking the
**most restrictive** of the two so the resolver's fine-grained allow/deny list is never loosened.

Behavior is preserved in the default (``standard``, non-strict) posture: a permissive shell baseline
allows ``shell.execute`` and ``network.request``, so the engine returns ``allow`` and the existing
resolver's decision stands verbatim. What the engine adds:

* ``locked`` mode denies ``shell.execute`` by default (no baseline allow) — the deny-by-default posture
  for CI / multi-tenant — until settings or a grant opens it.
* ``settings.json`` rules can deny/ask specific commands or hosts.
* Every command is audited (PP-6).

Network handling is deliberately honest: detecting ``curl``/``wget``/… by parsing the command line is
**best-effort, not a security boundary** (the design's real egress control is a sandbox/proxy, out of
scope here). It is off by default and only tightens under ``TABVIS_PERMISSION_BASH_STRICT``, where a
network command falls to ``ask`` and can be granted per host.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

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

_STRICT_ENV = "TABVIS_PERMISSION_BASH_STRICT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Known network binaries — heuristic only (see module docstring). Not exhaustive, not a boundary.
_NETWORK_BINS = frozenset({"curl", "wget", "nc", "ncat", "netcat", "telnet", "ftp", "sftp", "scp", "ssh", "rsync"})

_RESTRICTIVENESS = {"deny": 3, "ask": 2, "passthrough": 1, "allow": 0}


def is_bash_strict() -> bool:
    val = os.environ.get(_STRICT_ENV)
    return bool(val) and val.strip().lower() in _TRUTHY


def _bash_engine(context: Any) -> PolicyEngine:
    mode = resolve_mode()
    agent_id = getattr(context, "agent_id", None) if context is not None else None
    rules: list[PolicyRule] = baseline_rules_for_mode(mode)
    if mode != "locked":
        # Preserve today's posture outside locked: shell execution is allowed at the action level;
        # fine-grained per-command control stays with the existing bash resolver.
        rules += compile_rules([{"id": "bash-shell-default", "effect": "allow", "actions": ["shell.execute"], "resources": ["**"]}])
        if not is_bash_strict():
            rules += compile_rules([{"id": "bash-network-default", "effect": "allow", "actions": ["network.request"], "resources": ["**"]}])
    rules += load_policy_rules_from_settings()
    rules += grant_store.active_rules(agent_id)
    return PolicyEngine(rules, fallback_for_mode(mode), mode=mode)


def _command_root(command: str) -> str:
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.strip().split()
    return os.path.basename(toks[0]) if toks else ""


def _network_targets(command: str) -> list[str]:
    """Best-effort ``url:`` resources for a command that looks like it makes a network request."""
    try:
        toks = shlex.split(command)
    except ValueError:
        toks = command.strip().split()
    roots = {os.path.basename(t) for t in toks}
    if not (roots & _NETWORK_BINS):
        return []
    urls = [t for t in toks if t.startswith(("http://", "https://"))]
    return [f"url:{u}" for u in urls] or ["url:unknown"]


def _checks(command: str) -> list[tuple[str, str]]:
    """The (action, resource) pairs a command must clear: shell.execute plus any network targets."""
    checks: list[tuple[str, str]] = [("shell.execute", f"shell:{_command_root(command)}")]
    checks += [("network.request", res) for res in _network_targets(command)]
    return checks


def _to_permission_decision(effect: str, rule_id: str | None, action: str, resource: str, input: Any) -> PermissionDecision:
    if effect == "deny":
        detail = f" by rule {rule_id!r}" if rule_id else ""
        return {
            "behavior": "deny",
            "message": (
                f"Tabvis blocked {action} on {resource}{detail} (permission policy). This is an "
                f"absolute deny; to allow it, remove or amend that rule."
            ),
            "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource},
        }
    if effect == "ask":
        return {
            "behavior": "ask",
            "message": (
                f"Tabvis wants to run a command requiring {action} on {resource} under the current "
                f"permission policy. Approving is remembered for this agent (scoped grant)."
            ),
            "updatedInput": input,
            "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource},
        }
    return {"behavior": "allow", "updatedInput": input}


def _emit_audit(context: Any, action: str, resource: str, effect: str, rule_id: str | None, mode: str, shadowed: bool) -> None:
    reason = f"matched rule {rule_id!r}" if rule_id else "no rule matched (mode fallback)"
    if shadowed:
        reason += f"; shadow mode served as allow (would be {effect})"
    policy_audit.emit(
        PolicyDecisionEvent(
            effect=effect, action=action, resource=resource, mode=mode, rule_id=rule_id, reason=reason,
            request_id=getattr(context, "tool_use_id", None) if context is not None else None,
            agent_id=getattr(context, "agent_id", None) if context is not None else None,
        )
    )


def evaluate_command(command: str, context: Any, input: Any = None) -> PermissionDecision:
    """Engine-only decision for a command: most restrictive across shell.execute + network checks."""
    engine = _bash_engine(context)
    worst = None  # (rank, effect, rule_id, action, resource)
    for action, resource in _checks(command):
        d = engine.evaluate(action, resource)
        rank = _RESTRICTIVENESS.get(d.effect, 0)
        if worst is None or rank > worst[0]:
            worst = (rank, d.effect, d.matched_rule_id, action, resource)

    _, effect, rule_id, action, resource = worst  # type: ignore[misc]
    mode = engine._mode  # noqa: SLF001 - internal read for the audit record
    shadowed = is_shadow_mode() and effect != "allow"
    _emit_audit(context, action, resource, effect, rule_id, mode, shadowed)
    if shadowed:
        return {"behavior": "allow", "updatedInput": input, "decisionReason": {"shadow": True, "wouldBe": effect}}
    return _to_permission_decision(effect, rule_id, action, resource, input)


async def evaluate(input: Any, context: Any) -> PermissionDecision:
    """Compose the existing bash resolver with the engine, taking the most restrictive decision."""
    from tabvis.agent.tools.bash_permissions import bash_tool_has_permission

    existing = await bash_tool_has_permission(input, context)
    command = input.command if hasattr(input, "command") else (input or {}).get("command", "")
    engine = evaluate_command(command, context, input)

    existing_rank = _RESTRICTIVENESS.get(existing.get("behavior", "allow"), 0)
    engine_rank = _RESTRICTIVENESS.get(engine.get("behavior", "allow"), 0)
    # Existing resolver wins ties — it carries the richer, command-specific message/suggestions.
    return existing if existing_rank >= engine_rank else engine
