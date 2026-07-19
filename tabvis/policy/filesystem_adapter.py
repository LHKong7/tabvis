"""Filesystem adapter over the unified permission policy engine (PP-7).

``docs/permission-policy-engine_v1.md`` §7 (Filesystem). Maps a file tool call to a ``(action,
resource)`` pair — read/write/delete are distinct actions, and the path is classified into a policy
resource by :func:`tabvis.policy.fs_resource.classify_path` (realpath-resolved, symlink-safe).

Behavior is preserved conservatively: a **permissive FS baseline** allows read/write/delete on the
workspace, session, and out-of-tree (``fs:``) paths, so ordinary file operations are unchanged. The
one new, hard gate is **absolute protection of ``config:`` writes/deletes** (``.env``, keystores,
browser profile, config home) — added as a ``deny`` rule that applies in *every* mode (deny is
absolute), because a config-home write must not be grantable even under ``trusted``. Config *reads*
are intentionally left allowed in this slice (the app legitimately reads ``.env``); gating them is a
later refinement.

Rule order (low→high): mode baseline → FS baseline → hard config-protect → settings → grants. As in
the browser adapter, ``deny`` wins regardless of position and grants layer highest.
"""

from __future__ import annotations

from typing import Any

from tabvis.policy import audit as policy_audit
from tabvis.policy import grants as grant_store
from tabvis.policy.audit import PolicyDecisionEvent
from tabvis.policy.engine import PolicyEngine
from tabvis.policy.fs_resource import classify_path
from tabvis.policy.modes import baseline_rules_for_mode, fallback_for_mode
from tabvis.policy.rules import PolicyRule, compile_rules
from tabvis.policy.settings_source import (
    is_shadow_mode,
    load_policy_rules_from_settings,
    resolve_mode,
)
from tabvis.types.permissions import PermissionDecision

import os

# Lenient FS baseline (default): read/write/delete on workspace / session / out-of-tree, and reads of
# config + secret paths (the app legitimately reads .env). Writes to config/secret are denied below.
_FS_BASELINE_LENIENT = [
    {
        "id": "fs-rw-default",
        "effect": "allow",
        "actions": ["filesystem.read", "filesystem.write", "filesystem.delete"],
        "resources": ["workspace:**", "session:**", "fs:**"],
    },
    {
        "id": "fs-config-secret-read",
        "effect": "allow",
        "actions": ["filesystem.read"],
        "resources": ["config:**", "secret:**"],
    },
]

# Strict FS baseline (TABVIS_PERMISSION_FS_STRICT): out-of-tree (fs:) writes need a directory grant, and
# secret reads are NOT allowed here (they fall to the strict read-protect deny below). General config
# reads stay allowed.
_FS_BASELINE_STRICT = [
    {
        "id": "fs-rw-workspace",
        "effect": "allow",
        "actions": ["filesystem.read", "filesystem.write", "filesystem.delete"],
        "resources": ["workspace:**", "session:**"],
    },
    {"id": "fs-config-read", "effect": "allow", "actions": ["filesystem.read"], "resources": ["config:**"]},
]

# Hard, mode-independent protection: writes/deletes to config and secret paths are denied everywhere.
_FS_WRITE_PROTECT = [
    {
        "id": "fs-protect-config",
        "effect": "deny",
        "actions": ["filesystem.write", "filesystem.delete"],
        "resources": ["config:**", "secret:**"],
    },
]

# Strict-only: deny reads of secret files (.env, keystores, storage-state, profile).
_FS_READ_PROTECT_STRICT = [
    {"id": "fs-protect-secret-read", "effect": "deny", "actions": ["filesystem.read"], "resources": ["secret:**"]},
]

_STRICT_ENV = "TABVIS_PERMISSION_FS_STRICT"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def is_fs_strict() -> bool:
    """Whether strict filesystem mode is on (``TABVIS_PERMISSION_FS_STRICT``, default off)."""
    val = os.environ.get(_STRICT_ENV)
    return bool(val) and val.strip().lower() in _TRUTHY


def _fs_engine(context: Any) -> PolicyEngine:
    mode = resolve_mode()
    agent_id = getattr(context, "agent_id", None) if context is not None else None
    strict = is_fs_strict()
    rules: list[PolicyRule] = baseline_rules_for_mode(mode)
    rules += compile_rules(_FS_BASELINE_STRICT if strict else _FS_BASELINE_LENIENT)
    rules += compile_rules(_FS_WRITE_PROTECT)
    if strict:
        rules += compile_rules(_FS_READ_PROTECT_STRICT)
    rules += load_policy_rules_from_settings()
    rules += grant_store.active_rules(agent_id)
    return PolicyEngine(rules, fallback_for_mode(mode), mode=mode)


def _to_permission_decision(effect: str, rule_id: str | None, action: str, resource: str, input: Any) -> PermissionDecision:
    if effect == "allow":
        return {"behavior": "allow", "updatedInput": input}
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
    return {
        "behavior": "ask",
        "message": (
            f"Tabvis wants to perform {action} on {resource}, which requires approval under the "
            f"current permission policy. Approving is remembered for this agent (scoped grant)."
        ),
        "updatedInput": input,
        "decisionReason": {"type": "rule", "rule": rule_id, "action": action, "resource": resource},
    }


def _apply_shadow(decision: PermissionDecision, input: Any) -> PermissionDecision:
    if not is_shadow_mode() or decision.get("behavior") == "allow":
        return decision
    reason = dict(decision.get("decisionReason") or {})
    reason.update({"shadow": True, "wouldBe": decision.get("behavior")})
    return {"behavior": "allow", "updatedInput": input, "decisionReason": reason}


def _emit_audit(context: Any, action: str, resource: str, effect: str, rule_id: str | None, mode: str, served: PermissionDecision) -> None:
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


def evaluate_path(action: str, path: str, context: Any, input: Any = None) -> PermissionDecision:
    """Evaluate a filesystem ``action`` on ``path``: classify → engine → audit → (shadow) decision."""
    resource = classify_path(path)
    decision = _fs_engine(context).evaluate(action, resource)
    result = _to_permission_decision(decision.effect, decision.matched_rule_id, action, resource, input)
    served = _apply_shadow(result, input)
    _emit_audit(context, action, resource, decision.effect, decision.matched_rule_id, decision.mode, served)
    return served


class PolicyDenied(PermissionError):
    """Raised at the write side-effect point when the re-check denies the operation (PP-7 hardening)."""


def enforce_write(path: str, context: Any, *, action: str = "filesystem.write") -> None:
    """Re-check ``path`` at the moment of writing and raise :class:`PolicyDenied` if not permitted.

    This closes the TOCTOU gap between ``check_permissions`` (which runs earlier, on the input path)
    and the actual disk write: if the path was swapped for a symlink into a protected area in between,
    :func:`classify_path` now resolves the *real* target and the engine blocks it. Shadow mode never
    raises (audit-only). ``ask`` at the write point is treated as deny — there is no one to prompt
    inside the side-effect path.
    """
    resource = classify_path(path)
    decision = _fs_engine(context).evaluate(action, resource)
    served_effect = "allow" if is_shadow_mode() else decision.effect
    _emit_audit(
        context,
        action,
        resource,
        decision.effect,
        decision.matched_rule_id,
        decision.mode,
        {"behavior": served_effect, "decisionReason": {"shadow": is_shadow_mode()} if is_shadow_mode() else {}},
    )
    if served_effect == "allow":
        return
    raise PolicyDenied(
        f"Filesystem policy blocked {action} on {resource} "
        f"(rule {decision.matched_rule_id!r}) at the write point."
    )


def grant_directory(
    abs_dir: str,
    context: Any,
    *,
    ttl_seconds: float | None = None,
) -> list[Any]:
    """Open a directory subtree for read/write/delete via scoped grants (for strict mode).

    Classifies ``abs_dir`` and records a grant per filesystem action over ``<resource>/**`` scoped to
    the context's agent. This is how a user explicitly attaches a working directory outside the
    workspace when strict mode would otherwise ask/deny it. Grants over ``config:``/``secret:`` are
    refused — protected areas are not openable this way (deny is absolute regardless, but this makes
    the refusal explicit rather than a silently-useless grant).
    """
    agent_id = getattr(context, "agent_id", None) if context is not None else None
    resource = classify_path(abs_dir)
    ns = resource.split(":", 1)[0]
    if ns in ("config", "secret"):
        raise PolicyDenied(f"cannot grant a protected directory ({resource})")
    pattern = f"{resource.rstrip('/')}/**"
    out = []
    for action in ("filesystem.read", "filesystem.write", "filesystem.delete"):
        out.append(
            grant_store.add_grant(action, pattern, agent_id=agent_id, ttl_seconds=ttl_seconds, reason="opened directory")
        )
    return out
