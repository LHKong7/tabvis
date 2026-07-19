"""Permission-rule disk loading + persistence.

Reads permission rules from settings sources (honouring ``allowManagedPermissionRulesOnly`` in
policy settings) and adds / deletes rules in the editable sources. Source precedence: when
``allowManagedPermissionRulesOnly`` is set, ONLY ``policySettings`` rules are returned;
otherwise every enabled source contributes (low -> high priority via
:func:`get_enabled_setting_sources`).

Rule round-tripping uses :func:`permission_rule_value_from_string` /
:func:`permission_rule_value_to_string`, so duplicate-detection and deletion normalise legacy
names (e.g. ``"KillShell"`` -> ``"TaskStop"``) to their canonical form before comparing.

The TS write path goes through ``updateSettingsForSource``; the headless skeleton has no shared
settings-writer, so a minimal faithful writer (:func:`_update_settings_for_source`) lives here —
array values replace wholesale (the caller computes the final list), JSON is written with
two-space indent + trailing newline, and the session settings cache is reset after each write.

Casing: Python identifiers snake_case; the ``permissions`` lists and rule strings round-trip to
settings JSON, so wire keys (``allow`` / ``deny`` / ``ask``) stay verbatim, as do the parsed
rule-value keys (``toolName`` / ``ruleContent``).
"""

from __future__ import annotations

import json
import os
from typing import Any

from tabvis.types.permissions import (
    PermissionBehavior,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
)
from tabvis.utils.permissions.permission_rule_parser import (
    permission_rule_value_from_string,
    permission_rule_value_to_string,
)
from tabvis.utils.settings.constants import (
    EditableSettingSource,
    SettingSource,
    get_enabled_setting_sources,
    get_settings_file_path_for_source,
)
from tabvis.utils.settings.settings import (
    get_settings_for_source,
    reset_settings_cache,
)

__all__ = [
    "add_permission_rules_to_settings",
    "delete_permission_rule_from_settings",
    "get_permission_rules_for_source",
    "load_all_permission_rules_from_disk",
    "should_allow_managed_permission_rules_only",
    "should_show_always_allow_options",
]

class Error(Exception):
    """Lightweight error sentinel mirroring the TS ``{ error: Error | null }`` channel."""


# Behaviors loaded into rule objects, in fixed order (TS ``SUPPORTED_RULE_BEHAVIORS``).
_SUPPORTED_RULE_BEHAVIORS: tuple[PermissionBehavior, ...] = ("allow", "deny", "ask")

# Editable sources that can be modified (excludes policySettings and flagSettings).
_EDITABLE_SOURCES: tuple[EditableSettingSource, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
)


def should_allow_managed_permission_rules_only() -> bool:
    """Whether ``allowManagedPermissionRulesOnly`` is enabled in managed (policy) settings.

    When enabled, ONLY permission rules from managed settings are respected.
    """
    return (
        get_settings_for_source("policySettings").get("allowManagedPermissionRulesOnly")
        is True
    )


def should_show_always_allow_options() -> bool:
    """Whether "always allow" options should be shown in permission prompts.

    Hidden when :func:`should_allow_managed_permission_rules_only` is enabled.
    """
    return not should_allow_managed_permission_rules_only()


def _settings_json_to_rules(
    data: dict[str, Any] | None,
    source: PermissionRuleSource,
) -> list[PermissionRule]:
    """Convert a parsed settings dict to :data:`PermissionRule` objects.

    Reads the ``permissions.{allow,deny,ask}`` lists in :data:`_SUPPORTED_RULE_BEHAVIORS` order
    and parses each entry via :func:`permission_rule_value_from_string`.
    """
    if not data or not data.get("permissions"):
        return []

    permissions = data["permissions"]
    rules: list[PermissionRule] = []
    for behavior in _SUPPORTED_RULE_BEHAVIORS:
        behavior_array = permissions.get(behavior)
        if behavior_array:
            for rule_string in behavior_array:
                rules.append(
                    {
                        "source": source,
                        "ruleBehavior": behavior,
                        "ruleValue": permission_rule_value_from_string(rule_string),
                    }
                )
    return rules


def get_permission_rules_for_source(source: SettingSource) -> list[PermissionRule]:
    """Load permission rules from a single ``source``."""
    settings_data = get_settings_for_source(source)
    return _settings_json_to_rules(settings_data, source)


def load_all_permission_rules_from_disk() -> list[PermissionRule]:
    """Load all permission rules from every relevant source.

    Source precedence: if ``allowManagedPermissionRulesOnly`` is set, ONLY managed
    (``policySettings``) rules are used; otherwise every enabled source contributes in
    low -> high priority order (backwards compatible).
    """
    # If allowManagedPermissionRulesOnly is set, only use managed permission rules.
    if should_allow_managed_permission_rules_only():
        return get_permission_rules_for_source("policySettings")

    # Otherwise, load from all enabled sources (backwards compatible).
    rules: list[PermissionRule] = []
    for source in get_enabled_setting_sources():
        rules.extend(get_permission_rules_for_source(source))
    return rules


def _normalize_entry(raw: str) -> str:
    """Roundtrip-normalise a raw settings entry (legacy name -> canonical form)."""
    return permission_rule_value_to_string(permission_rule_value_from_string(raw))


def _update_settings_for_source(
    source: EditableSettingSource,
    settings: dict[str, Any],
) -> Error | None:
    """Update the settings for source.

    Array values replace wholesale (the caller computes the final list); a ``None`` value deletes
    the key. Writes JSON with two-space indent + a trailing newline, creating parent dirs, then
    resets the session settings cache. Policy / flag sources are no-ops. Returns an ``Error`` on
    failure, else ``None``.
    """
    if source in ("policySettings", "flagSettings"):
        return None

    file_path = get_settings_file_path_for_source(source)
    if not file_path:
        return None

    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        existing = get_settings_for_source(source) or {}
        updated = _merge_with_array_replace(existing, settings)

        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(updated, indent=2) + "\n")

        # Invalidate the session cache since settings have been updated.
        reset_settings_cache()
    except OSError as exc:
        return Error(f"Failed to write settings to {file_path}: {exc}")

    return None


def _merge_with_array_replace(
    target: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge ``source`` into a copy of ``target`` (TS ``mergeWith`` customizer semantics).

    - ``None`` source value -> delete the key.
    - list source value -> replace wholesale (caller owns the final state).
    - two dicts -> recurse.
    - otherwise -> source wins.
    """
    result = dict(target)
    for key, src_val in source.items():
        if src_val is None:
            result.pop(key, None)
            continue
        if isinstance(src_val, list):
            result[key] = src_val
            continue
        tgt_val = result.get(key)
        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            result[key] = _merge_with_array_replace(tgt_val, src_val)
            continue
        result[key] = src_val
    return result


def delete_permission_rule_from_settings(rule: PermissionRule) -> bool:
    """Delete a rule from its (editable) settings file.

    Returns ``True`` on success. No-ops (``False``) when the source is not editable, the rule is
    absent, or the write fails. Existing entries are roundtrip-normalised before comparison so
    legacy names match their canonical form.
    """
    source = rule["source"]
    # Runtime check to ensure source is actually editable.
    if source not in _EDITABLE_SOURCES:
        return False

    rule_string = permission_rule_value_to_string(rule["ruleValue"])
    settings_data = get_settings_for_source(source)

    # If there's no settings data or permissions, nothing to do.
    if not settings_data or not settings_data.get("permissions"):
        return False

    rule_behavior = rule["ruleBehavior"]
    behavior_array = settings_data["permissions"].get(rule_behavior)
    if not behavior_array:
        return False

    if not any(_normalize_entry(raw) == rule_string for raw in behavior_array):
        return False

    try:
        # Keep a copy of the original permissions data to preserve unrecognized keys.
        updated_settings_data = {
            **settings_data,
            "permissions": {
                **settings_data["permissions"],
                rule_behavior: [
                    raw for raw in behavior_array if _normalize_entry(raw) != rule_string
                ],
            },
        }

        error = _update_settings_for_source(source, updated_settings_data)
        if error:
            return False
        return True
    except OSError:
        return False


def add_permission_rules_to_settings(
    rule_values: list[PermissionRuleValue],
    rule_behavior: PermissionBehavior,
    source: EditableSettingSource,
) -> bool:
    """Add rules to an editable settings source.

    Returns ``True`` on success (including the no-op cases: empty input, or every rule already
    present). Returns ``False`` when ``allowManagedPermissionRulesOnly`` is set, or the write
    fails. Existing entries are roundtrip-normalised so legacy names dedup against canonical form.
    """
    # When allowManagedPermissionRulesOnly is enabled, don't persist new permission rules.
    if should_allow_managed_permission_rules_only():
        return False

    if len(rule_values) < 1:
        # No rules to add.
        return True

    rule_strings = [permission_rule_value_to_string(rv) for rv in rule_values]
    settings_data = get_settings_for_source(source) or {"permissions": {}}

    try:
        # Ensure permissions object exists.
        existing_permissions = settings_data.get("permissions") or {}
        existing_rules = existing_permissions.get(rule_behavior) or []

        # Filter out duplicates - normalize existing entries via roundtrip parse->serialize so
        # legacy names match their canonical form.
        existing_rules_set = {_normalize_entry(raw) for raw in existing_rules}
        new_rules = [r for r in rule_strings if r not in existing_rules_set]

        # If no new rules to add, return success.
        if not new_rules:
            return True

        # Keep a copy of the original settings data to preserve unrecognized keys.
        updated_settings_data = {
            **settings_data,
            "permissions": {
                **existing_permissions,
                rule_behavior: [*existing_rules, *new_rules],
            },
        }
        error = _update_settings_for_source(source, updated_settings_data)
        if error:
            return False
        return True
    except OSError:
        return False
