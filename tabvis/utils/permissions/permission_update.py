"""Apply + persist permission updates.

Applies a :data:`PermissionUpdate` (the discriminated union defined in
:mod:`tabvis.types.permissions`) to an in-memory
:class:`~tabvis.types.permissions.ToolPermissionContext` and (optionally) persists it to the backing
settings source. Also builds Read-rule suggestions for additional working directories.

Casing: Python identifiers are snake_case; the dict-shaped ``PermissionUpdate`` / context payloads
round-trip to settings / transcript / SDK JSON, so wire keys are kept verbatim — the update
discriminant ``type`` (``addRules`` / ``setMode`` / …), ``behavior`` / ``destination`` / ``mode`` /
``rules`` / ``directories``, the nested rule-value keys (``toolName`` / ``ruleContent``), and the
context collections (``alwaysAllowRules`` / ``alwaysDenyRules`` / ``alwaysAskRules`` /
``additionalWorkingDirectories``).

Import-cycle break: ``PermissionUpdate`` ↔ ``filesystem`` form a cycle in the TS tree
(``filesystem`` imports :func:`create_read_rule_suggestion` from here; here we need
``filesystem.to_posix_path``). The Python ``tabvis.utils.permissions.filesystem`` is implemented later in
this workflow, so :func:`create_read_rule_suggestion` does a **function-local lazy import** of it,
and only :data:`TYPE_CHECKING` refs are pulled in at module scope. This module therefore imports
standalone even before ``filesystem`` exists.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any

from tabvis.types.permissions import (
    AdditionalWorkingDirectory,
    PermissionRuleValue,
    PermissionUpdate,
    PermissionUpdateDestination,
    ToolPermissionContext,
    WorkingDirectorySource,
)
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.permissions.permission_rule_parser import (
    permission_rule_value_from_string,
    permission_rule_value_to_string,
)
from tabvis.utils.permissions.permissions_loader import (
    add_permission_rules_to_settings,
)
from tabvis.utils.settings.settings import get_settings_for_source
from tabvis.utils.slow_operations import json_stringify

if TYPE_CHECKING:
    from tabvis.utils.settings.constants import EditableSettingSource

# Re-export for backwards compatibility (parity with the TS re-export block).
__all__ = [
    "AdditionalWorkingDirectory",
    "WorkingDirectorySource",
    "apply_permission_update",
    "apply_permission_updates",
    "create_read_rule_suggestion",
    "extract_rules",
    "has_rules",
    "persist_permission_update",
    "persist_permission_updates",
    "supports_persistence",
]


def extract_rules(
    updates: list[PermissionUpdate] | None,
) -> list[PermissionRuleValue]:
    """Flatten every ``addRules`` update's ``rules`` into a single list (TS ``flatMap``)."""
    if not updates:
        return []

    rules: list[PermissionRuleValue] = []
    for update in updates:
        if update.get("type") == "addRules":
            rules.extend(update.get("rules", []))
    return rules


def has_rules(updates: list[PermissionUpdate] | None) -> bool:
    return len(extract_rules(updates)) > 0


def _rule_kind_for_behavior(behavior: str) -> str:
    """Map a ``behavior`` (``allow`` / ``deny`` / ``ask``) to the context collection key."""
    if behavior == "allow":
        return "alwaysAllowRules"
    if behavior == "deny":
        return "alwaysDenyRules"
    return "alwaysAskRules"


def apply_permission_update(
    context: ToolPermissionContext,
    update: PermissionUpdate,
) -> ToolPermissionContext:
    """Apply a single permission update to ``context`` and return the (new) updated context.

    The input ``context`` is not mutated — a shallow copy is returned with the affected
    collection replaced (parity with the TS ``{ ...context, ... }`` spreads).
    """
    update_type = update.get("type")

    if update_type == "setMode":
        log_for_debugging(
            f"Applying permission update: Setting mode to '{update['mode']}'",
        )
        return {**context, "mode": update["mode"]}

    if update_type == "addRules":
        rule_strings = [permission_rule_value_to_string(rule) for rule in update["rules"]]
        log_for_debugging(
            "Applying permission update: Adding "
            f"{len(update['rules'])} {update['behavior']} rule(s) to destination "
            f"'{update['destination']}': {json_stringify(rule_strings)}",
        )

        rule_kind = _rule_kind_for_behavior(update["behavior"])
        destination = update["destination"]
        existing = context.get(rule_kind, {})  # type: ignore[call-overload]
        return {
            **context,
            rule_kind: {
                **existing,
                destination: [*(existing.get(destination) or []), *rule_strings],
            },
        }

    if update_type == "replaceRules":
        rule_strings = [permission_rule_value_to_string(rule) for rule in update["rules"]]
        log_for_debugging(
            f"Replacing all {update['behavior']} rules for destination "
            f"'{update['destination']}' with {len(update['rules'])} rule(s): "
            f"{json_stringify(rule_strings)}",
        )

        rule_kind = _rule_kind_for_behavior(update["behavior"])
        destination = update["destination"]
        existing = context.get(rule_kind, {})  # type: ignore[call-overload]
        return {
            **context,
            rule_kind: {
                **existing,
                # Replace all rules for this source.
                destination: rule_strings,
            },
        }

    if update_type == "addDirectories":
        directories = update["directories"]
        log_for_debugging(
            f"Applying permission update: Adding {len(directories)} "
            f"director{'y' if len(directories) == 1 else 'ies'} with destination "
            f"'{update['destination']}': {json_stringify(directories)}",
        )
        # TS uses a Map; the Python context models additionalWorkingDirectories as a plain dict.
        new_additional_dirs: dict[str, AdditionalWorkingDirectory] = dict(
            context.get("additionalWorkingDirectories", {})
        )
        for directory in directories:
            new_additional_dirs[directory] = {
                "path": directory,
                "source": update["destination"],
            }
        return {**context, "additionalWorkingDirectories": new_additional_dirs}

    if update_type == "removeRules":
        rule_strings = [permission_rule_value_to_string(rule) for rule in update["rules"]]
        log_for_debugging(
            f"Applying permission update: Removing {len(update['rules'])} "
            f"{update['behavior']} rule(s) from source '{update['destination']}': "
            f"{json_stringify(rule_strings)}",
        )

        rule_kind = _rule_kind_for_behavior(update["behavior"])
        destination = update["destination"]
        existing = context.get(rule_kind, {})  # type: ignore[call-overload]
        existing_rules = existing.get(destination) or []
        rules_to_remove = set(rule_strings)
        filtered_rules = [r for r in existing_rules if r not in rules_to_remove]
        return {
            **context,
            rule_kind: {
                **existing,
                destination: filtered_rules,
            },
        }

    if update_type == "removeDirectories":
        directories = update["directories"]
        log_for_debugging(
            f"Applying permission update: Removing {len(directories)} "
            f"director{'y' if len(directories) == 1 else 'ies'}: "
            f"{json_stringify(directories)}",
        )
        new_additional_dirs = dict(context.get("additionalWorkingDirectories", {}))
        for directory in directories:
            new_additional_dirs.pop(directory, None)
        return {**context, "additionalWorkingDirectories": new_additional_dirs}

    return context


def apply_permission_updates(
    context: ToolPermissionContext,
    updates: list[PermissionUpdate],
) -> ToolPermissionContext:
    """Apply multiple permission updates in order, threading the result through."""
    updated_context = context
    for update in updates:
        updated_context = apply_permission_update(updated_context, update)
    return updated_context


def supports_persistence(destination: PermissionUpdateDestination) -> bool:
    """Type guard: whether ``destination`` is an editable (persistable) settings source.

    TS narrows to ``EditableSettingSource`` (``localSettings`` / ``userSettings`` /
    ``projectSettings``).
    """
    return destination in ("localSettings", "userSettings", "projectSettings")


def persist_permission_update(update: PermissionUpdate) -> None:
    """Persist a permission update to the appropriate (editable) settings source.

    No-op when the destination is not persistable (e.g. ``session`` / ``cliArg``). Directory /
    replace-rule / mode persistence writes via the minimal settings writer (the TS
    ``updateSettingsForSource``); rule additions go through
    :func:`add_permission_rules_to_settings`.
    """
    destination = update["destination"]
    if not supports_persistence(destination):
        return

    log_for_debugging(
        f"Persisting permission update: {update['type']} to source '{destination}'",
    )

    update_type = update["type"]

    if update_type == "addRules":
        log_for_debugging(
            f"Persisting {len(update['rules'])} {update['behavior']} rule(s) to {destination}",
        )
        add_permission_rules_to_settings(
            rule_values=update["rules"],
            rule_behavior=update["behavior"],
            source=destination,
        )
        return

    if update_type == "addDirectories":
        directories = update["directories"]
        log_for_debugging(
            f"Persisting {len(directories)} "
            f"director{'y' if len(directories) == 1 else 'ies'} to {destination}",
        )
        existing_settings = get_settings_for_source(destination)
        existing_dirs = (
            (existing_settings or {}).get("permissions", {}).get("additionalDirectories") or []
        )

        # Add new directories, avoiding duplicates.
        dirs_to_add = [d for d in directories if d not in existing_dirs]
        if dirs_to_add:
            updated_dirs = [*existing_dirs, *dirs_to_add]
            _update_settings_for_source(
                destination,
                {"permissions": {"additionalDirectories": updated_dirs}},
            )
        return

    if update_type == "removeRules":
        log_for_debugging(
            f"Removing {len(update['rules'])} {update['behavior']} rule(s) from {destination}",
        )
        existing_settings = get_settings_for_source(destination)
        existing_permissions = (existing_settings or {}).get("permissions") or {}
        existing_rules = existing_permissions.get(update["behavior"]) or []

        # Convert rules to normalized strings for comparison. Normalize via parse->serialize
        # roundtrip so "Bash(*)" and "Bash" match.
        rules_to_remove = {permission_rule_value_to_string(r) for r in update["rules"]}
        filtered_rules = [
            rule
            for rule in existing_rules
            if permission_rule_value_to_string(permission_rule_value_from_string(rule))
            not in rules_to_remove
        ]
        _update_settings_for_source(
            destination,
            {"permissions": {update["behavior"]: filtered_rules}},
        )
        return

    if update_type == "removeDirectories":
        directories = update["directories"]
        log_for_debugging(
            f"Removing {len(directories)} "
            f"director{'y' if len(directories) == 1 else 'ies'} from {destination}",
        )
        existing_settings = get_settings_for_source(destination)
        existing_dirs = (
            (existing_settings or {}).get("permissions", {}).get("additionalDirectories") or []
        )

        # Remove specified directories.
        dirs_to_remove = set(directories)
        filtered_dirs = [d for d in existing_dirs if d not in dirs_to_remove]
        _update_settings_for_source(
            destination,
            {"permissions": {"additionalDirectories": filtered_dirs}},
        )
        return

    if update_type == "setMode":
        log_for_debugging(f"Persisting mode '{update['mode']}' to {destination}")
        _update_settings_for_source(
            destination,
            {"permissions": {"defaultMode": update["mode"]}},
        )
        return

    if update_type == "replaceRules":
        log_for_debugging(
            f"Replacing all {update['behavior']} rules in {destination} with "
            f"{len(update['rules'])} rule(s)",
        )
        rule_strings = [permission_rule_value_to_string(r) for r in update["rules"]]
        _update_settings_for_source(
            destination,
            {"permissions": {update["behavior"]: rule_strings}},
        )
        return


def persist_permission_updates(updates: list[PermissionUpdate]) -> None:
    """Persist multiple permission updates (only those with persistable sources)."""
    for update in updates:
        persist_permission_update(update)


def create_read_rule_suggestion(
    dir_path: str,
    destination: PermissionUpdateDestination = "session",
) -> PermissionUpdate | None:
    """Create a Read-rule ``addRules`` suggestion for ``dir_path``.

    Returns ``None`` for the root directory (too broad). For absolute paths an extra leading ``/``
    is prepended to build a ``//path/**`` pattern.

    Lazy-imports :func:`tabvis.utils.permissions.filesystem.to_posix_path` to break the
    ``PermissionUpdate`` ↔ ``filesystem`` import cycle (``filesystem`` is implemented later).
    """
    # Function-local import breaks the PermissionUpdate <-> filesystem cycle.
    from tabvis.utils.permissions.filesystem import to_posix_path

    # Convert to POSIX format for pattern matching (handles Windows internally).
    path_for_pattern = to_posix_path(dir_path)

    # Root directory is too broad to be a reasonable permission target.
    if path_for_pattern == "/":
        return None

    # For absolute paths, prepend an extra / to create //path/** pattern.
    if posixpath.isabs(path_for_pattern):
        rule_content = f"/{path_for_pattern}/**"
    else:
        rule_content = f"{path_for_pattern}/**"

    return {
        "type": "addRules",
        "rules": [
            {
                "toolName": "Read",
                "ruleContent": rule_content,
            },
        ],
        "behavior": "allow",
        "destination": destination,
    }


def _update_settings_for_source(
    source: EditableSettingSource,
    settings: dict[str, Any],
) -> None:
    """Update the settings for source.

    Mirrors the writer already living in
    :mod:`tabvis.utils.permissions.permissions_loader` (the shared settings writer is not exposed
    by ``tabvis.utils.settings.settings``): plain-dict values deep-merge, list values replace
    wholesale (the caller computes the final list), JSON is written with two-space indent + a
    trailing newline, and the session settings cache is reset after each write. Policy / flag
    sources are no-ops.
    """
    import json
    import os

    from tabvis.utils.settings.constants import get_settings_file_path_for_source
    from tabvis.utils.settings.settings import reset_settings_cache

    if source in ("policySettings", "flagSettings"):
        return

    file_path = get_settings_file_path_for_source(source)
    if not file_path:
        return

    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        existing = get_settings_for_source(source) or {}
        updated = _merge_with_array_replace(existing, settings)
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(updated, indent=2) + "\n")
        # Invalidate the session cache since settings have been updated.
        reset_settings_cache()
    except OSError:
        return


def _merge_with_array_replace(
    target: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge ``source`` into a copy of ``target`` (TS ``mergeWith`` customizer semantics).

    ``None`` deletes the key; lists replace wholesale; two dicts recurse; otherwise source wins.
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
