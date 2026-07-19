"""Working-directory tracking"""

from __future__ import annotations

import os

_ORIGINAL_CWD = os.getcwd()


def get_original_cwd() -> str:
    return _ORIGINAL_CWD


def get_cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return _ORIGINAL_CWD


def get_project_root() -> str:
    """Stable project root (set at startup to the resolved cwd).

    Skeleton: the bootstrap-state worktree/``--worktree`` overrides are not implemented, so
    this is the original startup cwd.
    """
    return _ORIGINAL_CWD
