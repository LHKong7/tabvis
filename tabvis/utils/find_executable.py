"""Find an executable by searching PATH

:func:`find_executable` resolves a command name to its full path via :func:`tabvis.utils.which.
which_sync` (a ``which``-style lookup), returning a ``{cmd, args}`` dict that mirrors the
``spawn-rx`` ``findActualExecutable`` API shape — the TS replacement that avoids pulling in rxjs.

``cmd`` is the resolved path if found, or the original name if not; ``args`` is always the
pass-through of the input args.

Wire-shape note (per ``docs/SPINE_CONTRACTS.md``): the return is a plain runtime ``dict`` with
the ``cmd`` / ``args`` keys kept verbatim to match the ``spawn-rx`` shape its callers expect.
"""

from __future__ import annotations

from tabvis.utils.which import which_sync


def find_executable(exe: str, args: list[str]) -> dict[str, object]:
    """Find ``exe`` on PATH, returning ``{"cmd": <resolved-or-original>, "args": args}``."""
    resolved = which_sync(exe)
    return {"cmd": resolved if resolved is not None else exe, "args": args}
