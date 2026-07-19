"""Auto-memory paths.

``is_auto_memory_enabled`` applies env/SIMPLE gates plus ``settings.auto_memory_enabled``
(default ON). ``get_auto_mem_path`` resolves ``TABVIS_MEMORY_PATH_OVERRIDE`` first, then the
``settings.auto_memory_directory`` override, else
``<base>/projects/<sanitized-git-root>/memory/``. When there is no git repo, the memory base falls
back to the project root (cwd).
"""

from __future__ import annotations

import os
import re
import unicodedata

from tabvis.utils.cwd import get_project_root
from tabvis.utils.env_utils import (
    get_tabvis_config_home_dir,
    is_env_defined_falsy,
    is_env_truthy,
)
from tabvis.utils.git import find_canonical_git_root
from tabvis.utils.path import sanitize_path

AUTO_MEM_DIRNAME = "memory"


def is_auto_memory_enabled() -> bool:
    """Whether auto-memory features are enabled. Enabled by default."""
    env_val = os.environ.get("TABVIS_DISABLE_AUTO_MEMORY")
    if is_env_truthy(env_val):
        return False
    if is_env_defined_falsy(env_val):
        return True
    if is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
        return False
    if (
        is_env_truthy(os.environ.get("TABVIS_REMOTE"))
        and not os.environ.get("TABVIS_REMOTE_MEMORY_DIR")
        and not get_auto_mem_path_override()
    ):
        return False
    # settings.auto_memory_enabled override — explicit project-level opt-out.
    from tabvis.utils.settings.settings import get_initial_settings

    auto_memory_enabled = get_initial_settings().auto_memory_enabled
    if auto_memory_enabled is not None:
        return auto_memory_enabled
    return True


def get_memory_base_dir() -> str:
    """Base directory for persistent memory storage (``~/.tabvis`` by default)."""
    override = os.environ.get("TABVIS_REMOTE_MEMORY_DIR")
    if override:
        return override
    return get_tabvis_config_home_dir()


def _get_auto_mem_base() -> str:
    """Canonical git repo root if available, else the stable project root."""
    return find_canonical_git_root(get_project_root()) or get_project_root()


def _validate_memory_path(raw: str | None, expand_tilde: bool) -> str | None:
    """Sanitize a settings.json directory override.

    Returns the normalized absolute path (trailing separator, NFC) or ``None`` for empty, relative,
    root/drive-root, UNC, or NUL-containing values. Settings paths support ``~/`` expansion.
    """
    if not raw:
        return None
    candidate = raw
    if expand_tilde and (candidate.startswith("~/") or candidate.startswith("~\\")):
        rest = candidate[2:]
        rest_norm = os.path.normpath(rest or ".")
        if rest_norm in (".", ".."):
            return None
        candidate = os.path.join(os.path.expanduser("~"), rest)
    normalized = os.path.normpath(candidate).rstrip("/\\")
    if (
        not os.path.isabs(normalized)
        or len(normalized) < 3
        or re.fullmatch(r"[A-Za-z]:", normalized)
        or normalized.startswith("\\\\")
        or normalized.startswith("//")
        or "\0" in normalized
    ):
        return None
    return unicodedata.normalize("NFC", normalized + os.sep)


def _get_auto_mem_path_setting() -> str | None:
    """Settings.json ``autoMemoryDirectory`` override, validated with ~/ expansion."""
    from tabvis.utils.settings.settings import get_initial_settings

    return _validate_memory_path(get_initial_settings().auto_memory_directory, True)


def get_auto_mem_path_override() -> str | None:
    """Validated ``TABVIS_MEMORY_PATH_OVERRIDE`` value.

    This is an environment/API-provided path, so it must already be absolute; unlike the user
    setting, ``~/`` is not expanded.
    """
    return _validate_memory_path(os.environ.get("TABVIS_MEMORY_PATH_OVERRIDE"), False)


def has_auto_mem_path_override() -> bool:
    """Whether a valid ``TABVIS_MEMORY_PATH_OVERRIDE`` is active."""
    return get_auto_mem_path_override() is not None


def get_auto_mem_path() -> str:
    """Return the auto-memory directory.

    Resolution order: ``TABVIS_MEMORY_PATH_OVERRIDE``; settings.json ``autoMemoryDirectory``;
    ``<memoryBase>/projects/<sanitized-git-root>/memory/``. The returned path has a trailing
    separator.
    """
    override = get_auto_mem_path_override() or _get_auto_mem_path_setting()
    if override:
        return override
    projects_dir = os.path.join(get_memory_base_dir(), "projects")
    path = (
        os.path.join(projects_dir, sanitize_path(_get_auto_mem_base()), AUTO_MEM_DIRNAME)
        + os.sep
    )
    return unicodedata.normalize("NFC", path)
