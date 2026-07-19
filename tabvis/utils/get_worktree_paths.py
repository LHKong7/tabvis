"""Git worktree path listing (CLI variant)

:func:`get_worktree_paths` runs ``git worktree list --porcelain`` and returns the absolute
worktree paths (current worktree first, then the rest alphabetically). If git is unavailable,
not in a git repo, or there is only one worktree, it returns ``[]``.

This is the CLI variant (the ``git_exe()`` resolver). For a portable
version see :func:`tabvis.utils.get_worktree_paths_portable.get_worktree_paths_portable`.

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``execFileNoThrowWithCwd`` → :func:`tabvis.utils.exec_file_no_throw.exec_file_no_throw_with_cwd`
  (the wrapper that always resolves; ``preserve_output_on_error=False`` matches the TS option).
- ``gitExe()`` → :func:`_git_exe` below: ``which_sync('git') or 'git'``, cached once.
- ``sep`` → :data:`os.sep`. ``.normalize('NFC')`` → :func:`unicodedata.normalize`. ``localeCompare``
  sort → a plain ``sorted`` (the worktree paths are filesystem paths; ordinary code-point ordering
  is the faithful, locale-independent stand-in).

Casing: Python identifiers are snake_case; the return value is a plain ``list[str]`` of native
paths — no wire-key dicts.
"""

from __future__ import annotations

import os
import time
import unicodedata

from tabvis.utils.exec_file_no_throw import exec_file_no_throw_with_cwd
from tabvis.utils.which import which_sync

_WORKTREE_PREFIX = "worktree "

# Cached git-executable resolver (lodash ``memoize`` over a zero-arg fn caches its first result).
_GIT_EXE_CACHED: str | None = None


def _git_exe() -> str:
    """Resolve the ``git`` executable path once and cache it."""
    global _GIT_EXE_CACHED
    if _GIT_EXE_CACHED is None:
        _GIT_EXE_CACHED = which_sync("git") or "git"
    return _GIT_EXE_CACHED


async def get_worktree_paths(cwd: str) -> list[str]:
    """Return the absolute worktree paths for the git repo containing ``cwd``.

    Current worktree first, then the others alphabetically. ``[]`` if git is unavailable, not in a
    repo, or there is only one worktree.
    """
    start_time = time.time() * 1000

    result = await exec_file_no_throw_with_cwd(
        _git_exe(),
        ["worktree", "list", "--porcelain"],
        {"cwd": cwd, "preserve_output_on_error": False},
    )

    duration_ms = int(time.time() * 1000 - start_time)

    if result["code"] != 0:
        return []

    stdout = result.get("stdout") or ""
    # Parse porcelain output — lines starting with "worktree " contain paths.
    worktree_paths = [
        unicodedata.normalize("NFC", line[len(_WORKTREE_PREFIX) :])
        for line in stdout.split("\n")
        if line.startswith(_WORKTREE_PREFIX)
    ]

    # Sort worktrees: current worktree first, then alphabetically.
    current_worktree = next(
        (p for p in worktree_paths if cwd == p or cwd.startswith(p + os.sep)),
        None,
    )
    other_worktrees = sorted(p for p in worktree_paths if p != current_worktree)

    return [current_worktree, *other_worktrees] if current_worktree is not None else other_worktrees
