"""Session setup.

One-shot startup wiring run before the first query: custom session id, teammate-mode snapshot,
terminal-backup restoration, cwd switch, hooks-config snapshot + FileChanged watcher, optional
worktree creation (+ tmux), background-job kicks, command prefetch, analytics sinks +
``tengu_started`` beacon, api-key prefetch, release-notes prefetch, the
``--dangerously-skip-permissions`` safety gate, and the last-session ``tengu_exit`` beacon.

Exits use ``sys.exit(1)``; diagnostic output is written via ``print(..., file=sys.stderr)``; ANSI
color formatting uses the local ``_red`` / ``_bold_red`` / ``_yellow`` / ``_green`` SGR helpers.
Several imports are function-local rather than module-level. ``getuid``/platform checks come from
:mod:`os` / :mod:`sys`.

A few pieces of startup wiring are not implemented in this build; this module provides local
behavior for them instead:

* :func:`_set_cwd` — ``os.chdir`` + best-effort :func:`tabvis.bootstrap.state.set_cwd_state`.
* :func:`_get_current_project_config` / :func:`_get_global_config` — return ``{}`` (project /
  global config read not supported).
* :func:`_lock_current_version` — no-op (version locking not supported).
"""

from __future__ import annotations

import os
from typing import Any

from tabvis.utils.env_utils import is_env_truthy

# ----------------------------------------------------------------------------------------------
# ANSI color shims (level-2 SGR) + fallbacks for startup steps not implemented in this build
# ----------------------------------------------------------------------------------------------


def _red(text: str) -> str:
    return f"\x1b[31m{text}\x1b[39m"


def _bold_red(text: str) -> str:
    return f"\x1b[1m\x1b[31m{text}\x1b[39m\x1b[22m"


def _bold(text: str) -> str:
    return f"\x1b[1m{text}\x1b[22m"


def _yellow(text: str) -> str:
    return f"\x1b[33m{text}\x1b[39m"


def _green(text: str) -> str:
    return f"\x1b[32m{text}\x1b[39m"


def is_bare_mode() -> bool:
    """Bare/scripted-mode gate — the ``TABVIS_BARE`` env truthiness check."""
    return is_env_truthy(os.environ.get("TABVIS_BARE"))


def _set_cwd(cwd: str) -> None:
    """chdir + best-effort bootstrap-state update: ``os.chdir`` then update the bootstrap cwd
    state if available.
    """
    try:
        os.chdir(cwd)
    except OSError:
        pass
    try:
        from tabvis.bootstrap.state import set_cwd_state

        set_cwd_state(cwd)
    except (ImportError, AttributeError):
        pass


def _get_current_project_config() -> dict[str, Any]:
    """Project config read is not supported in this build — returns an empty dict."""
    return {}


def _get_global_config() -> dict[str, Any]:
    """Global config read is not supported in this build — returns an empty dict."""
    return {}


def _lock_current_version() -> None:
    """Version locking is not supported in this build — no-op."""
