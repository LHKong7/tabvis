"""``browser-os-data/`` layout helpers (PERS-1).

The target storage tree from ``design.md`` §"数据存储"::

    browser-os-data/
    ├── runtime.db
    ├── identities/{identity_id}/{profile.snapshot, storage-state.enc}
    ├── workspaces/{workspace_id}/{artifacts, checkpoints, replays}
    ├── sessions/{session_id}/{working-profile, downloads}
    └── logs/

These helpers just resolve (and lazily create) that tree under the tabvis config home. Nothing writes
into it yet — today's real state still lives in the per-session dir and the Chromium profile dir
(see ``design.md`` §"数据存储" 当前实现). The root exists so PERS-2+ can populate it additively.
"""

from __future__ import annotations

import os

from tabvis.utils.env_utils import get_tabvis_config_home_dir

BROWSER_OS_DATA_DIRNAME = "browser-os-data"
RUNTIME_DB_FILENAME = "runtime.db"


def get_browser_os_data_dir() -> str:
    """``<config-home>/browser-os-data`` — the Browser OS data root (not created here)."""
    return os.path.join(get_tabvis_config_home_dir(), BROWSER_OS_DATA_DIRNAME)


def browser_os_data_subdir(*parts: str, create: bool = False) -> str:
    """Resolve a path under the data root; ``create=True`` makes the directory (lazy)."""
    path = os.path.join(get_browser_os_data_dir(), *parts)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def runtime_db_path() -> str:
    """``<data-root>/runtime.db`` — the (future) SQLite metadata store (PERS-2)."""
    return os.path.join(get_browser_os_data_dir(), RUNTIME_DB_FILENAME)


def identities_dir(identity_id: str | None = None, *, create: bool = False) -> str:
    """``identities/`` or ``identities/{identity_id}/``."""
    parts = ("identities",) if identity_id is None else ("identities", identity_id)
    return browser_os_data_subdir(*parts, create=create)


def workspaces_dir(workspace_id: str | None = None, *, create: bool = False) -> str:
    """``workspaces/`` or ``workspaces/{workspace_id}/``."""
    parts = ("workspaces",) if workspace_id is None else ("workspaces", workspace_id)
    return browser_os_data_subdir(*parts, create=create)


def sessions_dir(session_id: str | None = None, *, create: bool = False) -> str:
    """``sessions/`` or ``sessions/{session_id}/``."""
    parts = ("sessions",) if session_id is None else ("sessions", session_id)
    return browser_os_data_subdir(*parts, create=create)


def logs_dir(*, create: bool = False) -> str:
    """``logs/``."""
    return browser_os_data_subdir("logs", create=create)
