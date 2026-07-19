"""Permission rule helpers

Skeleton scope: ``get_deny_rule_for_tool`` (blanket-deny detection used by tool filtering) plus
the source-ordered rule extractors (:func:`get_allow_rules` / :func:`get_ask_rules` /
:func:`get_deny_rules`) and :func:`permission_rule_source_display_string`. The full rule matcher
(wildcard/content matching, MCP server-prefix rules, ask/allow resolution) is implemented in a later
wave.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tabvis.utils.settings.constants import (
    SETTING_SOURCES,
    get_setting_source_display_name_lowercase,
)

if TYPE_CHECKING:
    from tabvis.tool import Tool
    from tabvis.types.permissions import (
        PermissionRule,
        PermissionRuleSource,
        ToolPermissionContext,
    )

# Order matters: the on-disk setting sources (low -> high priority) followed by the
# permission-only runtime sources. Mirrors the TS ``PERMISSION_RULE_SOURCES`` spread.
PERMISSION_RULE_SOURCES: tuple[str, ...] = (
    *SETTING_SOURCES,
    "cliArg",
    "command",
    "session",
)


def permission_rule_source_display_string(source: PermissionRuleSource) -> str:
    """Lowercase display name for a permission-rule ``source`` (inline use)."""
    return get_setting_source_display_name_lowercase(source)


def _rules_for_behavior(
    rules_by_source: dict[str, list[str]],
    rule_behavior: str,
) -> list[PermissionRule]:
    """Flatten a ``{source: [ruleString, ...]}`` map into :data:`PermissionRule` dicts.

    Iterates sources in :data:`PERMISSION_RULE_SOURCES` order (matching the TS ``flatMap``) and
    parses each rule string via :func:`permission_rule_value_from_string`.
    """
    # Lazy import: permission_rule_parser pulls in tabvis.agent.tools.* tool-name constants, whose
    # package __init__ imports this module (get_deny_rule_for_tool). Importing at call time
    # breaks that load-order cycle.
    from tabvis.utils.permissions.permission_rule_parser import (
        permission_rule_value_from_string,
    )

    rules: list[PermissionRule] = []
    for source in PERMISSION_RULE_SOURCES:
        for rule_string in rules_by_source.get(source) or []:
            rules.append(
                {
                    "source": source,
                    "ruleBehavior": rule_behavior,
                    "ruleValue": permission_rule_value_from_string(rule_string),
                }
            )
    return rules


def get_allow_rules(context: ToolPermissionContext) -> list[PermissionRule]:
    """Return every allow rule in the context, source-ordered."""
    return _rules_for_behavior(context.get("alwaysAllowRules", {}), "allow")


def get_deny_rules(context: ToolPermissionContext) -> list[PermissionRule]:
    """Return every deny rule in the context, source-ordered."""
    return _rules_for_behavior(context.get("alwaysDenyRules", {}), "deny")


def get_ask_rules(context: ToolPermissionContext) -> list[PermissionRule]:
    """Return every ask rule in the context, source-ordered."""
    return _rules_for_behavior(context.get("alwaysAskRules", {}), "ask")


def get_deny_rule_for_tool(
    permission_context: ToolPermissionContext, tool: Tool
) -> PermissionRule | None:
    """Return a blanket deny rule for ``tool`` if one exists, else ``None``.

    A *blanket* deny is a rule whose value is exactly the tool name with no ``ruleContent``
    (e.g. ``"Bash"``, not ``"Bash(git *)"``). Such tools are stripped before the model sees
    them. Content-scoped rules are evaluated at call time (later wave).
    """
    deny_rules: dict[str, Any] = (
        permission_context.get("alwaysDenyRules", {})
        if isinstance(permission_context, dict)
        else {}
    )
    name = tool.name
    for source, rules in deny_rules.items():
        for rule in rules or []:
            if rule == name:  # bare tool name => blanket deny
                return {
                    "source": source,
                    "ruleBehavior": "deny",
                    "ruleValue": {"toolName": name},
                }
    return None
