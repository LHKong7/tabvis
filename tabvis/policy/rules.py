"""Policy rule model + compilation (PP-1).

``docs/permission-policy-engine_v1.md`` §5.3: a rule is ``{id, effect, actions[], resources[]}`` and
matches when the concrete action matches **any** of its ``actions`` AND the concrete resource matches
**any** of its ``resources``. Compilation validates shape eagerly — an invalid rule raises
:class:`PolicyConfigError` rather than being silently dropped (§8 PP-2 acceptance).

Pure module: no I/O, no global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tabvis.policy.actions import ACTION_CATEGORIES, action_matches
from tabvis.policy.resources import ResourceRef, normalize_resource, resource_matches

Effect = Literal["allow", "deny", "ask"]
EFFECTS: frozenset[str] = frozenset({"allow", "deny", "ask"})


class PolicyConfigError(ValueError):
    """A rule (or ruleset) is malformed — raised at compile time, never swallowed."""


@dataclass(frozen=True)
class PolicyRule:
    """One compiled policy rule."""

    id: str
    effect: Effect
    actions: tuple[str, ...]
    resources: tuple[str, ...]

    def matches(self, action: str, resource: ResourceRef) -> bool:
        """True if this rule covers ``(action, resource)`` — any action AND any resource."""
        if not any(action_matches(p, action) for p in self.actions):
            return False
        return any(resource_matches(p, resource) for p in self.resources)


def _validate_action_pattern(pattern: str, rule_id: str) -> None:
    if not isinstance(pattern, str) or not pattern:
        raise PolicyConfigError(f"rule {rule_id!r}: action pattern must be a non-empty string")
    if pattern in ("*", "**"):
        return
    category = pattern.split(".", 1)[0]
    if category not in ("*", "**") and category not in ACTION_CATEGORIES:
        raise PolicyConfigError(
            f"rule {rule_id!r}: unknown action category {category!r} "
            f"(known: {sorted(ACTION_CATEGORIES)})"
        )


def compile_rule(raw: dict[str, Any]) -> PolicyRule:
    """Validate and compile one raw rule dict into a :class:`PolicyRule`."""
    if not isinstance(raw, dict):
        raise PolicyConfigError(f"rule must be an object, got {type(raw).__name__}")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise PolicyConfigError("rule is missing a non-empty string 'id'")

    effect = raw.get("effect")
    if effect not in EFFECTS:
        raise PolicyConfigError(f"rule {rule_id!r}: effect must be one of {sorted(EFFECTS)}, got {effect!r}")

    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions:
        raise PolicyConfigError(f"rule {rule_id!r}: 'actions' must be a non-empty list")
    for pat in actions:
        _validate_action_pattern(pat, rule_id)

    resources = raw.get("resources")
    if not isinstance(resources, list) or not resources:
        raise PolicyConfigError(f"rule {rule_id!r}: 'resources' must be a non-empty list")
    for res in resources:
        if not isinstance(res, str) or not res:
            raise PolicyConfigError(f"rule {rule_id!r}: each resource must be a non-empty string")
        normalize_resource(res)  # exercise the parser; surfaces gross malformations early

    return PolicyRule(
        id=rule_id,
        effect=effect,  # type: ignore[arg-type]
        actions=tuple(actions),
        resources=tuple(resources),
    )


def compile_rules(raws: list[dict[str, Any]]) -> list[PolicyRule]:
    """Compile a list of raw rule dicts, rejecting duplicate ids."""
    seen: set[str] = set()
    compiled: list[PolicyRule] = []
    for raw in raws:
        rule = compile_rule(raw)
        if rule.id in seen:
            raise PolicyConfigError(f"duplicate rule id {rule.id!r}")
        seen.add(rule.id)
        compiled.append(rule)
    return compiled
