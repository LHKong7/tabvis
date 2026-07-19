"""Platform-agnostic ``ps``-style process helpers

Implements the cross-platform ``ps``/``pgrep`` probes Tabvis uses for lock recovery and process
introspection: liveness (signal-0), ancestor-PID and ancestor-command walks, single-process
command lookup, and child-PID enumeration.

When extending this file, handle: Win32 (``ps`` inside cygwin/WSL may not behave as expected,
especially reaching host processes) and Unix-vs-BSD ``ps`` option differences.

Casing: Python identifiers are snake_case. No wire-key dicts are involved (return values are
ints / strings / lists thereof).

Faithful-behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``process.kill(pid, 0)`` (existence probe) → :func:`os.kill` with signal ``0``. As in TS,
  ``EPERM`` (process exists but owned by another user) is reported as **not running** — the
  conservative choice for lock recovery (don't steal a live lock).
- ``execFileNoThrowWithCwd('sh', ['-c', script], {timeout})`` →
  :func:`tabvis.utils.exec_file_no_throw.exec_file_no_throw_with_cwd` with the *same* shell
  scripts (a single ``ps`` invocation walking the tree, null-byte-separated commands).
- ``execSyncWithDefaults_DEPRECATED(command, {timeout})`` →
  :func:`tabvis.utils.exec_file_no_throw.exec_sync_with_defaults_deprecated` (the re-exported sync
  shell-out). The deprecated ``getProcessCommand`` / ``getChildPids`` keep that synchronous shape.
- PowerShell branches preserved verbatim for ``win32`` parity (``process.platform === 'win32'``
  → ``sys.platform == 'win32'``).
"""

from __future__ import annotations

import os
import sys

from tabvis.utils.exec_file_no_throw import (
    exec_file_no_throw_with_cwd,
    exec_sync_with_defaults_deprecated,
)

_DEFAULT_MAX_DEPTH = 10
_WALK_TIMEOUT_MS = 3000
_SYNC_TIMEOUT_MS = 1000

PidLike = str | int


def is_process_running(pid: int) -> bool:
    """Whether a process with ``pid`` is running (signal-0 probe).

    ``pid <= 1`` returns ``False`` (0 is the current process group, 1 is init). As in TS, an
    ``EPERM`` (process owned by another user) is reported as NOT running — conservative for lock
    recovery.
    """
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _parse_pids(lines: list[str]) -> list[int]:
    """``.filter(Boolean).map(parseInt).filter(!isNaN)`` over an already-split list."""
    out: list[int] = []
    for raw in lines:
        token = raw.strip()
        if not token:
            continue
        try:
            # int(token, 10) raises on non-numeric — matches the isNaN drop.
            out.append(int(token, 10))
        except ValueError:
            continue
    return out


async def get_ancestor_pids_async(
    pid: PidLike,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> list[int]:
    """Ancestor PID chain for ``pid`` (immediate parent → furthest ancestor, up to ``max_depth``)."""
    if sys.platform == "win32":
        # Windows: a PowerShell script that walks the process tree.
        script = (
            f"""
      $pid = {pid!s}
      $ancestors = @()
      for ($i = 0; $i -lt {max_depth}; $i++) {{
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pid" -ErrorAction SilentlyContinue
        if (-not $proc -or -not $proc.ParentProcessId -or $proc.ParentProcessId -eq 0) {{ break }}
        $pid = $proc.ParentProcessId
        $ancestors += $pid
      }}
      $ancestors -join ','
    """
        ).strip()

        result = await exec_file_no_throw_with_cwd(
            "powershell.exe",
            ["-NoProfile", "-Command", script],
            {"timeout": _WALK_TIMEOUT_MS},
        )
        stdout = result.get("stdout") or ""
        if result["code"] != 0 or not stdout.strip():
            return []
        return _parse_pids(stdout.strip().split(","))

    # Unix: a shell command that walks up the process tree in a single invocation.
    script = (
        f"pid={pid!s}; for i in $(seq 1 {max_depth}); do "
        "ppid=$(ps -o ppid= -p $pid 2>/dev/null | tr -d ' '); "
        'if [ -z "$ppid" ] || [ "$ppid" = "0" ] || [ "$ppid" = "1" ]; then break; fi; '
        "echo $ppid; pid=$ppid; done"
    )

    result = await exec_file_no_throw_with_cwd(
        "sh",
        ["-c", script],
        {"timeout": _WALK_TIMEOUT_MS},
    )
    stdout = result.get("stdout") or ""
    if result["code"] != 0 or not stdout.strip():
        return []
    return _parse_pids(stdout.strip().split("\n"))


def get_process_command(pid: PidLike) -> str | None:
    """Command line for ``pid``, or ``None`` if not found.

    Deprecated: prefer :func:`get_ancestor_commands_async`.
    """
    try:
        pid_str = str(pid)
        if sys.platform == "win32":
            command = (
                "powershell.exe -NoProfile -Command "
                f'"(Get-CimInstance Win32_Process -Filter \\"ProcessId={pid_str}\\").CommandLine"'
            )
        else:
            command = f"ps -o command= -p {pid_str}"

        result = exec_sync_with_defaults_deprecated(command, {"timeout": _SYNC_TIMEOUT_MS})
        return result.strip() if result else None
    except Exception:  # noqa: BLE001 - faithful TS catch-all
        return None


async def get_ancestor_commands_async(
    pid: PidLike,
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> list[str]:
    """Command lines for ``pid`` and its ancestors, collected in a single invocation."""
    if sys.platform == "win32":
        # Windows: walk the tree and collect command lines (null-byte separated).
        script = (
            f"""
      $currentPid = {pid!s}
      $commands = @()
      for ($i = 0; $i -lt {max_depth}; $i++) {{
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$currentPid" -ErrorAction SilentlyContinue
        if (-not $proc) {{ break }}
        if ($proc.CommandLine) {{ $commands += $proc.CommandLine }}
        if (-not $proc.ParentProcessId -or $proc.ParentProcessId -eq 0) {{ break }}
        $currentPid = $proc.ParentProcessId
      }}
      $commands -join [char]0
    """
        ).strip()

        result = await exec_file_no_throw_with_cwd(
            "powershell.exe",
            ["-NoProfile", "-Command", script],
            {"timeout": _WALK_TIMEOUT_MS},
        )
        stdout = result.get("stdout") or ""
        if result["code"] != 0 or not stdout.strip():
            return []
        return [s for s in stdout.split("\0") if s]

    # Unix: walk the tree and collect commands, null-byte separated (handles embedded newlines).
    script = (
        f"currentpid={pid!s}; for i in $(seq 1 {max_depth}); do "
        "cmd=$(ps -o command= -p $currentpid 2>/dev/null); "
        "if [ -n \"$cmd\" ]; then printf '%s\\0' \"$cmd\"; fi; "
        "ppid=$(ps -o ppid= -p $currentpid 2>/dev/null | tr -d ' '); "
        'if [ -z "$ppid" ] || [ "$ppid" = "0" ] || [ "$ppid" = "1" ]; then break; fi; '
        "currentpid=$ppid; done"
    )

    result = await exec_file_no_throw_with_cwd(
        "sh",
        ["-c", script],
        {"timeout": _WALK_TIMEOUT_MS},
    )
    stdout = result.get("stdout") or ""
    if result["code"] != 0 or not stdout.strip():
        return []
    return [s for s in stdout.split("\0") if s]


def get_child_pids(pid: PidLike) -> list[int]:
    """Child process IDs of ``pid`` (``pgrep -P`` on Unix, CIM query on Windows)."""
    try:
        pid_str = str(pid)
        if sys.platform == "win32":
            command = (
                "powershell.exe -NoProfile -Command "
                f'"(Get-CimInstance Win32_Process -Filter \\"ParentProcessId={pid_str}\\").ProcessId"'
            )
        else:
            command = f"pgrep -P {pid_str}"

        result = exec_sync_with_defaults_deprecated(command, {"timeout": _SYNC_TIMEOUT_MS})
        if not result:
            return []
        return _parse_pids(result.strip().split("\n"))
    except Exception:  # noqa: BLE001 - faithful TS catch-all
        return []
