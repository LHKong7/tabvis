"""Settings source identifiers + file-path helpers.

Only the pieces the headless
spine needs: the :data:`SettingSource` literal, the per-source settings-file path helpers, the
global ``~/.tabvis.json`` config path, and the enabled-source ordering (low -> high priority).

Casing: Python identifiers snake_case; the wire source names (``userSettings`` etc.) stay
camelCase because they are stable string keys that round-trip to settings UIs / the SDK.
"""

from __future__ import annotations

import os
from typing import Literal, get_args

from ..cwd import get_original_cwd
from ..env_utils import get_tabvis_config_home_dir

# All possible sources where settings can come from.
# Order matters — later sources override earlier ones (low -> high priority).
SettingSource = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "policySettings",
    "flagSettings",
]

# Ordered low -> high priority. Mirrors src/bootstrap/state.ts allowedSettingSources default
# (user, project, local, flag, policy). policySettings + flagSettings are always enabled.
SETTING_SOURCES: tuple[SettingSource, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
)

# Editable sources (policy + flag are read-only).
EditableSettingSource = Literal["userSettings", "projectSettings", "localSettings"]

# The JSON Schema URL for Tabvis settings (allowed value of the `$schema` key).
TABVIS_SETTINGS_SCHEMA_URL = "https://json.schemastore.org/tabvis-settings.json"


def get_user_settings_path() -> str:
    """``<config-home>/settings.json`` — global user settings (``~/.tabvis/settings.json``)."""
    return os.path.join(get_tabvis_config_home_dir(), "settings.json")


def get_project_settings_path(cwd: str | None = None) -> str:
    """``<cwd>/.tabvis/settings.json`` — shared per-project settings (checked in)."""
    root = cwd if cwd is not None else get_original_cwd()
    return os.path.join(root, ".tabvis", "settings.json")


def get_local_settings_path(cwd: str | None = None) -> str:
    """``<cwd>/.tabvis/settings.local.json`` — project-local settings (gitignored)."""
    root = cwd if cwd is not None else get_original_cwd()
    return os.path.join(root, ".tabvis", "settings.local.json")


def get_global_config_path() -> str:
    """Path to the global config file ``~/.tabvis.json``.

    ``$TABVIS_CONFIG_DIR`` or the home
    directory, joined with ``.tabvis.json``. NOTE: this is distinct from the settings config-home
    (``$TABVIS_CONFIG_DIR`` or ``~/.tabvis``) — the global config lives one level up, as a dotfile.
    """
    base = os.environ.get("TABVIS_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".tabvis.json")


def get_setting_source_display_name_lowercase(source: str) -> str:
    """Lowercase display name for a setting or permission-rule source (inline use).

    Return the setting source display name lowercase.
    Accepts the on-disk :data:`SettingSource` values plus the permission-only sources
    (``cliArg`` / ``command`` / ``session``). Unknown sources fall back to the raw string,
    standing in for the exhaustive-``switch`` contract on the TS side.
    """
    names = {
        "userSettings": "user settings",
        "projectSettings": "shared project settings",
        "localSettings": "project local settings",
        "flagSettings": "command line arguments",
        "policySettings": "enterprise managed settings",
        "cliArg": "CLI argument",
        "command": "command configuration",
        "session": "current session",
    }
    return names.get(source, source)


def get_settings_file_path_for_source(source: SettingSource) -> str | None:
    """Path to the settings file backing ``source``, or ``None`` when it has no file path.

    ``policySettings`` (managed/MDM) and ``flagSettings`` (CLI ``--settings``) are stubbed —
    they have no plain on-disk path in this build.
    """
    if source == "userSettings":
        return get_user_settings_path()
    if source == "projectSettings":
        return get_project_settings_path()
    if source == "localSettings":
        return get_local_settings_path()
    # policySettings / flagSettings — stubbed (no file path in the skeleton).
    return None


def get_enabled_setting_sources() -> list[SettingSource]:
    """Enabled sources, ordered low -> high priority (later overrides earlier).

    The allowed sources with ``policySettings`` and
    ``flagSettings`` always included. The skeleton has no runtime override of the allowed set,
    so this returns the full default ordering.
    """
    return list(SETTING_SOURCES)


def all_setting_sources() -> tuple[SettingSource, ...]:
    """Every declared :data:`SettingSource` value (for validation / iteration)."""
    return get_args(SettingSource)
