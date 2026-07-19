"""Settings loaders

Reads + merges the per-source settings files (user / project / local) into effective settings, with
a module-level session cache (mirroring ``settingsCache.ts`` + ``getSettingsWithErrors``).

Merge semantics replicate lodash ``mergeWith(..., settingsMergeCustomizer)``: deep-merge plain
dicts (e.g. ``permissions``, ``hooks``); for list values, concatenate + dedup (preserving order).
Sources merge low -> high priority, so later sources override earlier ones.

Not supported in this build: managed / MDM / policy sources, flag settings + inline SDK settings,
the full SettingsSchema validation pass, and the changeDetector / file watchers. ``policySettings``
and ``flagSettings`` contribute nothing.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .constants import (
    SettingSource,
    get_enabled_setting_sources,
    get_global_config_path,
    get_settings_file_path_for_source,
)
from .types import SettingsJson

# --- session cache (module-level; mirrors settingsCache.ts) ---------------------------------------

# None = cache miss; a dict = cached merged effective settings for this session.
_session_settings_cache: dict[str, Any] | None = None


def reset_settings_cache() -> None:
    """Invalidate the session settings cache."""
    global _session_settings_cache
    _session_settings_cache = None


# --- file reading ---------------------------------------------------------------------------------


def get_settings_for_source(source: SettingSource) -> dict[str, Any]:
    """Read + parse the settings file backing ``source``.

    Returns ``{}`` when the file is absent, empty, invalid JSON, or the source has no on-disk path
    (policy / flag sources are stubbed). Mirrors ``getSettingsForSource`` but returns a plain dict
    (no per-source pydantic validation in the skeleton — that is deferred).
    """
    path = get_settings_file_path_for_source(source)
    if not path:
        return {}

    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
        return {}

    if content.strip() == "":
        return {}

    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return {}

    if not isinstance(data, dict):
        return {}
    return data


def get_global_config() -> dict[str, Any]:
    """Read ``~/.tabvis.json``, ``{}`` if absent/invalid."""
    path = get_global_config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, OSError):
        return {}

    if content.strip() == "":
        return {}

    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return {}

    return data if isinstance(data, dict) else {}


# --- merging --------------------------------------------------------------------------------------


def _merge_lists(target: list[Any], source: list[Any]) -> list[Any]:
    """Concatenate + dedup, preserving first-seen order."""
    result: list[Any] = []
    for item in [*target, *source]:
        if item not in result:
            result.append(item)
    return result


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``source`` into ``target`` (lodash ``mergeWith`` + ``settingsMergeCustomizer``).

    - Two dicts -> recurse.
    - Two lists -> concatenate + dedup.
    - Otherwise -> ``source`` wins (later/higher-priority source overrides).

    Mutates and returns ``target``.
    """
    for key, src_val in source.items():
        if key in target:
            tgt_val = target[key]
            if isinstance(tgt_val, dict) and isinstance(src_val, dict):
                target[key] = _deep_merge(tgt_val, src_val)
                continue
            if isinstance(tgt_val, list) and isinstance(src_val, list):
                target[key] = _merge_lists(tgt_val, src_val)
                continue
        target[key] = src_val
    return target


# --- disk load + session-cached accessors ---------------------------------------------------------


def load_settings_from_disk() -> SettingsJson:
    """Merge enabled sources (low -> high priority) into effective settings.

    Reads each enabled source's file fresh and deep-merges in order, so later sources override
    earlier ones. Returns a :class:`SettingsJson` (``{}`` when no files exist).
    ``loadSettingsFromDisk`` (errors/diagnostics path omitted — validation is deferred).
    """
    merged: dict[str, Any] = {}
    seen_files: set[str] = set()

    for source in get_enabled_setting_sources():
        path = get_settings_file_path_for_source(source)
        # Skip a file already merged from another source (dedup by path).
        if path is not None:
            resolved = os.path.abspath(path)
            if resolved in seen_files:
                continue
            seen_files.add(resolved)

        settings = get_settings_for_source(source)
        if settings:
            merged = _deep_merge(merged, settings)

    return SettingsJson.model_validate(merged)


def get_settings_with_errors() -> dict[str, Any]:
    """Session-cached merged settings.

    Returns ``{"settings": <dict>, "errors": []}``. The validation/error channel is not
    implemented in this build — the error list is always empty.
    """
    global _session_settings_cache
    if _session_settings_cache is not None:
        return _session_settings_cache

    settings = load_settings_from_disk()
    result: dict[str, Any] = {
        "settings": settings.model_dump(by_alias=True, exclude_none=True),
        "errors": [],
    }
    _session_settings_cache = result
    return result


def get_initial_settings() -> SettingsJson:
    """Effective merged settings for this session.

    Always returns a :class:`SettingsJson`; ``{}`` (an empty model) when no settings files exist.
    Session-cached via :func:`get_settings_with_errors`.
    """
    cached = get_settings_with_errors()
    return SettingsJson.model_validate(cached.get("settings") or {})
