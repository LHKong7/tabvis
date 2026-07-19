"""Tabvis settings subsystem (BOUNDED core).

The pieces the headless spine needs to load effective settings
from disk. Re-exports the public surface so consumers can ``from tabvis.utils.settings import ...``.

Not supported in this build: managed/MDM + policy + flag sources, full SettingsSchema
validation, and the changeDetector / file watchers.
"""

from __future__ import annotations

from .constants import (
    TABVIS_SETTINGS_SCHEMA_URL,
    SETTING_SOURCES,
    EditableSettingSource,
    SettingSource,
    get_enabled_setting_sources,
    get_global_config_path,
    get_local_settings_path,
    get_project_settings_path,
    get_settings_file_path_for_source,
    get_user_settings_path,
)
from .settings import (
    get_global_config,
    get_initial_settings,
    get_settings_for_source,
    get_settings_with_errors,
    load_settings_from_disk,
    reset_settings_cache,
)
from .types import (
    HookCommand,
    HookMatcher,
    HooksSettings,
    PermissionsSettings,
    SettingsJson,
)

__all__ = [
    # constants
    "TABVIS_SETTINGS_SCHEMA_URL",
    "SETTING_SOURCES",
    "EditableSettingSource",
    "SettingSource",
    "get_enabled_setting_sources",
    "get_global_config_path",
    "get_local_settings_path",
    "get_project_settings_path",
    "get_settings_file_path_for_source",
    "get_user_settings_path",
    # types
    "HookCommand",
    "HookMatcher",
    "HooksSettings",
    "PermissionsSettings",
    "SettingsJson",
    # settings loaders
    "get_global_config",
    "get_initial_settings",
    "get_settings_for_source",
    "get_settings_with_errors",
    "load_settings_from_disk",
    "reset_settings_cache",
]
