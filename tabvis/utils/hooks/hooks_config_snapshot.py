"""Hooks-config snapshot + policy gating

Captures a startup snapshot of the effective hooks config (respecting the managed/policy gates) so
the rest of the session reads a stable value, and exposes the policy predicates that decide whether
only managed hooks (or no hooks at all) should run.

Module-level ``_initial_hooks_config`` mirrors the TS module-scope ``initialHooksConfig`` closure.

Bounded: ``policySettings`` (managed/MDM) has no on-disk path in the skeleton, so ``getSettingsForSource
('policySettings')`` returns ``{}`` and the managed-only / disable-all gates are effectively off.
``getSettings_DEPRECATED()`` (the merged-settings reader) maps to the existing cached merged settings.

Casing: Python identifiers snake_case; the hooks-config payload (event-name keys + matcher dicts)
is a verbatim wire dict.
"""

from __future__ import annotations

from typing import Any

# HooksSettings: {event: [matcher, ...]} — kept as a plain dict (verbatim wire keys).
HooksSettings = dict[str, list[dict[str, Any]]]

_initial_hooks_config: HooksSettings | None = None


def _policy_settings() -> dict[str, Any]:
    from tabvis.utils.settings.settings import get_settings_for_source

    return get_settings_for_source("policySettings")


def _merged_settings() -> dict[str, Any]:
    """The merged effective settings (TS ``getSettings_DEPRECATED``)."""
    from tabvis.utils.settings.settings import get_initial_settings

    # SettingsJson -> dict via alias dump (verbatim wire keys), so callers read camelCase keys
    # (``disableAllHooks``/``allowManagedHooksOnly``) and the ``hooks`` map verbatim.
    return get_initial_settings().model_dump(by_alias=True, exclude_none=True)


def _get_hooks_from_allowed_sources() -> HooksSettings:
    """Get hooks from allowed sources, applying the policy gates.

    - policy ``disableAllHooks`` -> ``{}``;
    - policy ``allowManagedHooksOnly`` -> only the policy hooks;
    - non-managed ``disableAllHooks`` -> only the policy hooks (non-managed cannot disable managed);
    - otherwise -> the merged hooks from all sources.
    """
    policy = _policy_settings()

    if policy.get("disableAllHooks") is True:
        return {}

    if policy.get("allowManagedHooksOnly") is True:
        return policy.get("hooks") or {}

    merged = _merged_settings()

    if merged.get("disableAllHooks") is True:
        return policy.get("hooks") or {}

    return merged.get("hooks") or {}


def should_allow_managed_hooks_only() -> bool:
    """Whether only managed hooks should run."""
    policy = _policy_settings()
    if policy.get("allowManagedHooksOnly") is True:
        return True
    # disableAllHooks set, but NOT from managed settings -> treat as managed-only.
    if _merged_settings().get("disableAllHooks") is True and policy.get("disableAllHooks") is not True:
        return True
    return False


def should_disable_all_hooks_including_managed() -> bool:
    """Return whether policy sets ``disableAllHooks: true`` for every hook source."""
    return _policy_settings().get("disableAllHooks") is True


def capture_hooks_config_snapshot() -> None:
    """Capture a snapshot of the current hooks configuration."""
    global _initial_hooks_config
    _initial_hooks_config = _get_hooks_from_allowed_sources()


def update_hooks_config_snapshot() -> None:
    """Refresh the snapshot after settings change.

    Resets the session settings cache first so the snapshot reads fresh settings from disk (the
    file watcher's stability threshold may not have elapsed).
    """
    from tabvis.utils.settings.settings_cache import reset_settings_cache

    reset_settings_cache()
    global _initial_hooks_config
    _initial_hooks_config = _get_hooks_from_allowed_sources()


def get_hooks_config_from_snapshot() -> HooksSettings | None:
    """Get the snapshot, capturing one on first access."""
    if _initial_hooks_config is None:
        capture_hooks_config_snapshot()
    return _initial_hooks_config


def reset_hooks_config_snapshot() -> None:
    """Reset the snapshot and SDK initialization state for tests."""
    global _initial_hooks_config
    _initial_hooks_config = None
    from tabvis.bootstrap.state import reset_sdk_init_state

    reset_sdk_init_state()
