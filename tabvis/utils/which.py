"""Resolve a command's full path via ``which`` / ``where.exe``

Exposes the async :func:`which` and the sync :func:`which_sync`, each returning the absolute path
to a command on PATH or ``None`` if it is not found. The TS module prefers ``Bun.which`` (no
process spawn) when running under Bun, and otherwise spawns the platform-appropriate lookup
(``where.exe`` on Windows, ``which`` on POSIX).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- There is no Bun runtime under CPython (``bunWhich`` is ``null``), so both functions take the
  spawn path — exactly the ``bunWhich ? … : whichNodeAsync`` / ``bunWhich ?? whichNodeSync``
  fallbacks the TS would pick in a non-Bun runtime.
- ``process.platform === 'win32'`` → ``sys.platform == "win32"``.
- The async ``whichNodeAsync`` uses ``execa(`which ${command}`, { shell: true, stderr: 'ignore',
  reject: false })`` → :func:`asyncio.create_subprocess_shell` with stderr to ``DEVNULL`` and no
  raise on non-zero exit; the sync ``whichNodeSync`` uses ``execSync_DEPRECATED`` (imported here
  per the implementation chain) with ``stdio: ['ignore','pipe','ignore']``.
- Both honor the exit-code / empty-stdout guard (``exitCode !== 0 || !stdout`` → ``null``) and
  return ``result.stdout.trim()`` (Windows: the first line of the possibly-multiline
  ``where.exe`` output).
"""

from __future__ import annotations

import asyncio
import re
import sys

from tabvis.utils.exec_sync_wrapper import exec_sync_deprecated

_CRLF_SPLIT = re.compile(r"\r?\n")


async def _which_node_async(command: str) -> str | None:
    if sys.platform == "win32":
        # On Windows, use where.exe and return the first result.
        proc = await asyncio.create_subprocess_shell(
            f"where.exe {command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout_b, _ = await proc.communicate()
        if proc.returncode != 0 or not stdout_b:
            return None
        # where.exe returns multiple paths separated by newlines; return the first.
        first = _CRLF_SPLIT.split(stdout_b.decode("utf-8", "replace").strip())[0]
        return first or None

    # On POSIX systems (macOS, Linux, WSL), use which.
    proc = await asyncio.create_subprocess_shell(
        f"which {command}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout_b, _ = await proc.communicate()
    if proc.returncode != 0 or not stdout_b:
        return None
    return stdout_b.decode("utf-8", "replace").strip()


def _which_node_sync(command: str) -> str | None:
    if sys.platform == "win32":
        try:
            result = exec_sync_deprecated(
                f"where.exe {command}",
                {"encoding": "utf-8", "stdio": ["ignore", "pipe", "ignore"]},
            )
            output = (result if isinstance(result, str) else result.decode("utf-8")).strip()
            first = _CRLF_SPLIT.split(output)[0]
            return first or None
        except Exception:  # noqa: BLE001 - TS `catch { return null }`
            return None

    try:
        result = exec_sync_deprecated(
            f"which {command}",
            {"encoding": "utf-8", "stdio": ["ignore", "pipe", "ignore"]},
        )
        text = (result if isinstance(result, str) else result.decode("utf-8")).strip()
        return text or None
    except Exception:  # noqa: BLE001 - TS `catch { return null }`
        return None


async def which(command: str) -> str | None:
    """Find the full path to a command executable.

    Spawns the platform-appropriate lookup (no Bun runtime under CPython). Returns the full path,
    or ``None`` if not found.
    """
    return await _which_node_async(command)


def which_sync(command: str) -> str | None:
    """Synchronous version of :func:`which`. Returns the full path, or ``None`` if not found."""
    return _which_node_sync(command)
