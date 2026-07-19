"""Unified permission policy engine (PP-1).

Pure, side-effect-free core described in ``docs/permission-policy-engine_v1.md``. Not yet wired into
any tool — the Browser adapter (PP-3) and other execution surfaces consume this later. See the design
doc §8 for the roadmap.
"""

from __future__ import annotations

from tabvis.policy.actions import ACTIONS, action_matches, is_known_action
from tabvis.policy.audit import (
    PolicyDecisionEvent,
    emit,
    is_audit_enabled,
    register_sink,
)
from tabvis.policy.engine import PolicyDecision, PolicyEngine
from tabvis.policy.modes import (
    MODES,
    Mode,
    baseline_rules_for_mode,
    fallback_for_mode,
    is_mode,
)
from tabvis.policy.resources import (
    ResourceRef,
    normalize_resource,
    resource_matches,
)
from tabvis.policy.rules import (
    EFFECTS,
    Effect,
    PolicyConfigError,
    PolicyRule,
    compile_rule,
    compile_rules,
)
from tabvis.policy import grants
from tabvis.policy.fs_resource import classify_path
from tabvis.policy.grants import Grant
from tabvis.policy.settings_source import (
    build_policy_engine,
    is_shadow_mode,
    load_policy_rules_from_settings,
    read_mode_from_settings,
    resolve_mode,
)

__all__ = [
    "ACTIONS",
    "action_matches",
    "is_known_action",
    "PolicyDecisionEvent",
    "emit",
    "is_audit_enabled",
    "register_sink",
    "PolicyDecision",
    "PolicyEngine",
    "MODES",
    "Mode",
    "baseline_rules_for_mode",
    "fallback_for_mode",
    "is_mode",
    "ResourceRef",
    "normalize_resource",
    "resource_matches",
    "EFFECTS",
    "Effect",
    "PolicyConfigError",
    "PolicyRule",
    "compile_rule",
    "compile_rules",
    "build_policy_engine",
    "load_policy_rules_from_settings",
    "read_mode_from_settings",
    "resolve_mode",
    "is_shadow_mode",
    "grants",
    "Grant",
    "classify_path",
]
