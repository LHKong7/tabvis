"""Settings caches

Three module-level caches keyed off the same session lifecycle (all invalidated by
:func:`reset_settings_cache`):

1. ``_session_settings_cache`` — the merged :class:`SettingsWithErrors` for this session.
2. ``_per_source_cache`` — per-:data:`SettingSource` parsed settings (``None`` = "no settings for
   this source"; *absent* = cache miss).
3. ``_parse_file_cache`` — path-keyed parsed-file results, deduping the disk read + validate that
   both ``get_settings_for_source`` and ``load_settings_from_disk`` perform on the same paths during
   startup.

The TS ``undefined`` (cache miss) vs ``null`` (cached "no settings") distinction is preserved via a
private :data:`_MISS` sentinel — :func:`get_cached_settings_for_source` returns :data:`_MISS` on a
miss and ``None`` (or a value) on a hit, mirroring ``has(source) ? get(source) : undefined``.

Casing: Python identifiers snake_case; the cached :class:`SettingsWithErrors` /
:class:`ValidationError` keep their wire field names (defined in :mod:`.validation`).
"""

from __future__ import annotations

from typing import Any

from .constants import SettingSource
from .types import SettingsJson
from .validation import SettingsWithErrors, ValidationError

# Sentinel distinguishing a cache MISS (TS ``undefined``) from a cached ``None`` (TS ``null``).
_MISS: Any = object()


# --- merged session cache -------------------------------------------------------------------------

_session_settings_cache: SettingsWithErrors | None = None


def get_session_settings_cache() -> SettingsWithErrors | None:
    """The cached merged :class:`SettingsWithErrors`, or ``None`` on a miss (``getSessionSettingsCache``)."""
    return _session_settings_cache


def set_session_settings_cache(value: SettingsWithErrors) -> None:
    """Store the merged session settings."""
    global _session_settings_cache
    _session_settings_cache = value


# --- per-source cache -----------------------------------------------------------------------------

# Maps a source to its parsed settings; ``None`` = cached "no settings for this source".
_per_source_cache: dict[SettingSource, SettingsJson | None] = {}


def get_cached_settings_for_source(source: SettingSource) -> Any:
    """Cached parsed settings for ``source``.

    Returns :data:`_MISS` on a cache miss (TS ``undefined``), or the cached value — which may be
    ``None`` (TS ``null`` = "no settings for this source").
    """
    return _per_source_cache.get(source, _MISS) if source in _per_source_cache else _MISS


def set_cached_settings_for_source(source: SettingSource, value: SettingsJson | None) -> None:
    """Cache the parsed settings for ``source``."""
    _per_source_cache[source] = value


# --- parsed-file cache ----------------------------------------------------------------------------


class ParsedSettings(dict):
    """Parsed-file cache entry.

    A plain dict with ``{"settings": SettingsJson | None, "errors": list[ValidationError]}``.
    Modelled as a ``dict`` subclass so it round-trips like the TS object literal.
    """

    def __init__(
        self,
        settings: SettingsJson | None,
        errors: list[ValidationError] | None = None,
    ) -> None:
        super().__init__(settings=settings, errors=errors or [])


# Path-keyed cache for parse-settings-file (dedupes the disk read + validate per startup).
_parse_file_cache: dict[str, ParsedSettings] = {}


def get_cached_parsed_file(path: str) -> ParsedSettings | None:
    """Cached parsed-file result for ``path``, or ``None`` on a miss (``getCachedParsedFile``)."""
    return _parse_file_cache.get(path)


def set_cached_parsed_file(path: str, value: ParsedSettings) -> None:
    """Cache the parsed-file result for ``path``."""
    _parse_file_cache[path] = value


# --- invalidation ---------------------------------------------------------------------------------


def reset_settings_cache() -> None:
    """Clear all three settings caches.

    Fired on a settings write, ``--add-dir``, or hooks refresh — the same triggers as the TS.
    """
    global _session_settings_cache
    _session_settings_cache = None
    _per_source_cache.clear()
    _parse_file_cache.clear()
