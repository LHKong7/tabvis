"""Git root detection

Skeleton scope: ``find_git_root`` (walk up looking for ``.git``), ``find_canonical_git_root``
(worktree-aware canonicalization collapses to ``find_git_root`` when there is no worktree),
and ``get_is_git``. The branch/remote/shallow-clone helpers are planned for a later implementation phase.
"""

from __future__ import annotations

import os
import unicodedata

from tabvis.utils.cwd import get_cwd


def find_git_root(start_path: str) -> str | None:
    """Walk up from ``start_path`` looking for a ``.git`` dir/file; None if not found."""
    current = os.path.abspath(start_path)
    while True:
        git_path = os.path.join(current, ".git")
        if os.path.isdir(git_path) or os.path.isfile(git_path):
            return unicodedata.normalize("NFC", current)
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def find_canonical_git_root(start_path: str) -> str | None:
    """Canonical (worktree-resolved) git root.

    Skeleton: the worktree back-link resolution collapses to ``find_git_root`` (no
    worktree present), so all worktrees of a repo would still share one identity.
    """
    return find_git_root(start_path)


def get_is_git() -> bool:
    """Whether the current working directory is inside a git repository."""
    return find_git_root(get_cwd()) is not None
