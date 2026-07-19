"""Portable git worktree path listing

The TS module is a single async helper, ``getWorktreePathsPortable(cwd)``, that shells out to
``git worktree list --porcelain`` with a 5s timeout and extracts the ``worktree <path>`` lines.
It is deliberately dependency-free (only Node's ``child_process``) so SDK-side callers
(``listSessionsImpl.ts``) can resolve worktree paths without dragging in the CLI's
``execa → cross-spawn → which`` chain.

Casing: Python identifiers are snake_case; this returns a plain ``list[str]`` of native paths
(NFC-normalized, matching the TS ``.normalize('NFC')``), so there are no wire-key dicts.

Faithful-behavior notes:
- The TS swallows *all* errors (spawn failure, non-zero exit, timeout) and returns ``[]``.
  ``execFileAsync`` rejects on a non-zero exit, so any of those land in the ``catch``. We
  mirror that: any exception or non-zero return code yields ``[]``.
- Empty stdout → ``[]`` (TS ``if (!stdout) return []``).
- ``git`` is invoked as a bare argv (no shell), exactly like ``execFile('git', [...])``.
"""

from __future__ import annotations

import asyncio
import unicodedata

_WORKTREE_PREFIX = "worktree "
_TIMEOUT_MS = 5000


async def get_worktree_paths_portable(cwd: str) -> list[str]:
    """Return the worktree paths reported by ``git worktree list --porcelain`` in ``cwd``.

    Mirrors the TS helper: runs ``git`` with a 5s timeout, returns ``[]`` on any failure
    (spawn error, non-zero exit, or timeout) or when stdout is empty. Each ``worktree <path>``
    line is stripped of its prefix and NFC-normalized.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "worktree",
            "list",
            "--porcelain",
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception:  # noqa: BLE001 - parity with the TS catch-all
        return []

    try:
        stdout_b, _stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_MS / 1000
        )
    except TimeoutError:
        # TS: execFileAsync rejects on timeout → catch → []. Reap the process first.
        # (Python 3.11+: asyncio.TimeoutError is an alias of the builtin TimeoutError.)
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return []
    except Exception:  # noqa: BLE001 - parity with the TS catch-all
        return []

    # execFileAsync rejects on a non-zero exit; replicate that as an empty result.
    if proc.returncode != 0:
        return []

    stdout = (stdout_b or b"").decode("utf-8", "replace")
    if not stdout:
        return []

    return [
        unicodedata.normalize("NFC", line[len(_WORKTREE_PREFIX) :])
        for line in stdout.split("\n")
        if line.startswith(_WORKTREE_PREFIX)
    ]
