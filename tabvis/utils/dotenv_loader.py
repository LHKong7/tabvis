"""Optional ``.env`` autoloading (via ``python-dotenv``).

tabvis reads all of its configuration from the process environment (the ``TABVIS_*`` namespace). To make
local setup ergonomic, :func:`load_env_files` loads ``.env`` files into ``os.environ`` **once, at
startup, before any ``TABVIS_*`` value is read** (called from :func:`tabvis.bootstrap_entry.main`).

Precedence (highest wins):

1. the **real process environment** — exported variables always beat ``.env`` values, and
2. the **project** ``.env`` (``<cwd>/.env``), then
3. the **user** ``.env`` (``<config-home>/.env``, i.e. ``~/.tabvis/.env``).

Loading uses ``override=False``, so a key already present in the environment (from a real export or
an earlier, higher-priority file) is never clobbered. Set ``TABVIS_DOTENV`` to load a single explicit
file instead of the default pair, or ``TABVIS_DISABLE_DOTENV=1`` to turn the feature off entirely.

The loader is best-effort: a missing file is skipped silently, and if ``python-dotenv`` is somehow
unavailable (e.g. a stripped binary) it is a no-op rather than an error.
"""

from __future__ import annotations

import os

from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy

__all__ = ["load_env_files"]


def load_env_files() -> list[str]:
    """Load ``.env`` file(s) into ``os.environ`` and return the paths actually loaded.

    See the module docstring for precedence + the ``TABVIS_DOTENV`` / ``TABVIS_DISABLE_DOTENV`` knobs.
    """
    # The disable switch must come from the *real* environment — it gates loading the files that
    # could otherwise set it, so reading it from a ``.env`` would be circular.
    if is_env_truthy(os.environ.get("TABVIS_DISABLE_DOTENV")):
        return []

    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv not installed / not bundled — feature is a no-op.
        return []

    explicit = os.environ.get("TABVIS_DOTENV")
    if explicit:
        candidates = [explicit]
    else:
        # Project ``.env`` first so it wins over the user ``.env`` (override=False keeps the first).
        candidates = [
            os.path.join(os.getcwd(), ".env"),
            os.path.join(get_tabvis_config_home_dir(), ".env"),
        ]

    loaded: list[str] = []
    for path in candidates:
        if path and os.path.isfile(path):
            # override=False: real env vars and earlier (higher-priority) files are never clobbered.
            load_dotenv(path, override=False)
            loaded.append(path)
    return loaded
