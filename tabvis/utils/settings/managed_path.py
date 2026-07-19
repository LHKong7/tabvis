"""Managed-settings directory path resolution

Resolves the platform-specific directory that holds enterprise-managed settings
(``managed-settings.json`` + the ``managed-settings.d/`` drop-in directory). Used by the MDM /
policy-settings layer.

lodash ``memoize`` (zero-arg) -> :func:`functools.lru_cache` (matching ``tabvis/utils/platform.py``);
the memos cache the first result for the process. Call :func:`reset_managed_path_cache_for_tests`
after monkeypatching ``USER_TYPE`` / ``TABVIS_MANAGED_SETTINGS_PATH`` / the platform to re-resolve.

Casing: Python identifiers snake_case; the returned directory paths are platform literals (verbatim
from the TS so they round-trip to the same on-disk locations).
"""

from __future__ import annotations

import functools
import os

from ..platform import get_platform


@functools.lru_cache(maxsize=1)
def get_managed_file_path() -> str:
    """Path to the managed-settings directory for the current platform.

    Honors the ant-only ``TABVIS_MANAGED_SETTINGS_PATH`` override
    (gated on ``USER_TYPE == 'ant'``) for testing/demos, else the platform default.
    """

    platform = get_platform()
    if platform == "macos":
        return "/Library/Application Support/Tabvis"
    if platform == "windows":
        return "C:\\Program Files\\Tabvis"
    return "/etc/tabvis"


@functools.lru_cache(maxsize=1)
def get_managed_settings_drop_in_dir() -> str:
    """Path to the ``managed-settings.d/`` drop-in directory.

    ``managed-settings.json`` is merged first (base), then files in this directory are merged
    alphabetically on top (drop-ins override base, later files win).
    """
    return os.path.join(get_managed_file_path(), "managed-settings.d")


def reset_managed_path_cache_for_tests() -> None:
    """Clear the memoized managed-path resolutions (test-only; not in the TS surface)."""
    get_managed_file_path.cache_clear()
    get_managed_settings_drop_in_dir.cache_clear()
