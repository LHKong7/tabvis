r"""Concurrent-session PID registry

Writes a per-process PID file under ``<config-home>/sessions/<pid>.json`` so ``tabvis ps`` /
ListPeers can enumerate live sessions, and counts live concurrent sessions (sweeping stale PID
files from crashed sessions).

Casing: Python identifiers are snake_case. The PID-file payload is a JSON document that
round-trips to disk, so its keys are kept **verbatim wire keys** (camelCase: ``sessionId`` /
``startedAt`` / ``updatedAt`` / ``waitingFor`` / ``messagingSocketPath`` / ``logPath``).

Faithful-behavior notes:
- The TS imports ``chmod`` / ``mkdir`` / ``readdir`` / ``readFile`` / ``unlink`` / ``writeFile``
  directly from ``fs/promises`` (NOT via the FsOperations façade), so this implementation uses stdlib
  :mod:`os` calls dispatched through :func:`asyncio.to_thread` — matching the TS's direct fs use.
- ``jsonParse`` / ``jsonStringify`` are the REAL implemented
  :mod:`tabvis.utils.slow_operations` helpers.
- ``getOriginalCwd`` / ``getSessionId`` / ``onSessionSwitch`` are the REAL implemented
  :mod:`tabvis.bootstrap.state` surfaces; ``registerCleanup`` /
  ``getTabvisConfigHomeDir`` / ``isProcessRunning`` / ``getPlatform`` / ``getAgentId`` /
  ``errorMessage`` / ``isFsInaccessible`` are the REAL implemented utils.
- The TS ``if (false) { … }`` blocks are dead-code-eliminated env-var reads in external builds.
  They are reproduced here as ``_EXTERNAL_BUILD = False`` guards so the gated behavior stays
  faithfully off (no ``TABVIS_SESSION_KIND`` / messaging-socket / activity-push in this build).
- The strict ``^\d+\.json$`` filename guard is preserved verbatim — ``parseInt`` lenient
  prefix-parsing would otherwise sweep e.g. ``2026-03-14_notes.md`` as PID 2026 (data loss).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import os.path as _osp
import re
from typing import Any, Literal

from tabvis.bootstrap.state import (
    get_original_cwd,
    get_session_id,
    on_session_switch,
)
from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.errors import get_errno_code, get_error_message
from tabvis.utils.generic_process_utils import is_process_running
from tabvis.utils.platform import get_platform
from tabvis.utils.slow_operations import json_parse, json_stringify
from tabvis.utils.teammate import get_agent_id

SessionKind = Literal["interactive", "bg", "daemon", "daemon-worker"]
SessionStatus = Literal["busy", "idle", "waiting"]

# The TS gates several env-var reads behind ``if (false)`` so the strings are DCE'd from external
# builds. This flag reproduces that: those branches stay dead in this build.
_EXTERNAL_BUILD = False

# Only ``<pid>.json`` filenames are candidates. See module docstring (CC-34210 data-loss guard).
_PID_FILE_RE = re.compile(r"^\d+\.json$")

# Codes ``isFsInaccessible`` covers (see ``errors.isFsInaccessible``). Local fallback until the
# full ``tabvis.utils.errors`` taxonomy lands.
_FS_INACCESSIBLE_CODES = frozenset({"ENOENT", "EACCES", "EPERM", "ENOTDIR", "ELOOP"})


def _is_fs_inaccessible(error: object) -> bool:
    """Whether ``error`` is an expected "nothing there / no access" filesystem error."""
    return get_errno_code(error) in _FS_INACCESSIBLE_CODES


def _get_sessions_dir() -> str:
    return _osp.join(get_tabvis_config_home_dir(), "sessions")


def _env_session_kind() -> SessionKind | None:
    """Kind override from env (set by the spawner). Gated off in external builds."""
    if _EXTERNAL_BUILD:
        k = os.environ.get("TABVIS_SESSION_KIND")
        if k in ("bg", "daemon", "daemon-worker"):
            return k  # type: ignore[return-value]
    return None


def is_bg_session() -> bool:
    """True when this REPL is running inside a ``tabvis --bg`` tmux session."""
    return _env_session_kind() == "bg"


async def register_session() -> bool:
    """Write a PID file for this session and register cleanup.

    Registers all top-level sessions (interactive CLI, SDK, bg/daemon spawns); skips
    teammates/subagents. Returns ``True`` if registered, ``False`` if skipped. Errors are logged
    to debug, never thrown.
    """
    if get_agent_id() is not None:
        return False

    kind: SessionKind = _env_session_kind() or "interactive"
    directory = _get_sessions_dir()
    pid = os.getpid()
    pid_file = _osp.join(directory, f"{pid}.json")

    async def _cleanup() -> None:
        with contextlib.suppress(FileNotFoundError, OSError):
            # ENOENT is fine (already deleted or never written).
            await asyncio.to_thread(os.unlink, pid_file)

    register_cleanup(_cleanup)

    try:
        payload: dict[str, Any] = {
            "pid": pid,
            "sessionId": get_session_id(),
            "cwd": get_original_cwd(),
            "startedAt": _now_ms(),
            "kind": kind,
            "entrypoint": os.environ.get("TABVIS_ENTRYPOINT"),
        }
        if _EXTERNAL_BUILD:
            payload["messagingSocketPath"] = os.environ.get("TABVIS_MESSAGING_SOCKET")
            payload["name"] = os.environ.get("TABVIS_SESSION_NAME")
            payload["logPath"] = os.environ.get("TABVIS_SESSION_LOG")
            payload["agent"] = os.environ.get("TABVIS_AGENT")

        def _write() -> None:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
            with open(pid_file, "w", encoding="utf8") as f:
                f.write(json_stringify(payload))

        await asyncio.to_thread(_write)

        # --resume / /resume mutates getSessionId() via switchSession. Without this, the PID
        # file's sessionId goes stale and ``tabvis ps`` reads the wrong transcript.
        def _on_switch(session_id: str) -> None:
            asyncio.ensure_future(_update_pid_file({"sessionId": session_id}))

        on_session_switch(_on_switch)
        return True
    except Exception as e:  # noqa: BLE001 - faithful TS catch-all
        log_for_debugging(f"[concurrentSessions] register failed: {get_error_message(e)}")
        return False


async def _update_pid_file(patch: dict[str, Any]) -> None:
    """Merge ``patch`` into this session's PID file. Best-effort: silently no-op on any failure."""
    pid_file = _osp.join(_get_sessions_dir(), f"{os.getpid()}.json")
    try:

        def _rw() -> None:
            with open(pid_file, encoding="utf8") as f:
                data = json_parse(f.read())
            merged = {**data, **patch}
            with open(pid_file, "w", encoding="utf8") as f:
                f.write(json_stringify(merged))

        await asyncio.to_thread(_rw)
    except Exception as e:  # noqa: BLE001 - faithful TS catch-all
        log_for_debugging(
            f"[concurrentSessions] updatePidFile failed: {get_error_message(e)}"
        )


async def update_session_name(name: str | None) -> None:
    """Update this session's name in its PID registry file so ListPeers can surface it."""
    if not name:
        return
    await _update_pid_file({"name": name})


async def update_session_activity(patch: dict[str, Any]) -> None:
    """Push live activity state for ``tabvis ps``. Fire-and-forget; gated off in external builds.

    ``patch`` carries optional ``status`` (:data:`SessionStatus`) / ``waitingFor`` keys.
    """
    if not _EXTERNAL_BUILD:
        return
    await _update_pid_file({**patch, "updatedAt": _now_ms()})


async def count_concurrent_sessions() -> int:
    """Count live concurrent CLI sessions (including this one).

    Filters out stale PID files (crashed sessions) and deletes them. Returns ``0`` on any error
    (conservative).
    """
    directory = _get_sessions_dir()
    try:
        files = await asyncio.to_thread(os.listdir, directory)
    except Exception as e:  # noqa: BLE001 - faithful TS catch-all
        if not _is_fs_inaccessible(e):
            log_for_debugging(
                f"[concurrentSessions] readdir failed: {get_error_message(e)}"
            )
        return 0

    count = 0
    own_pid = os.getpid()
    platform = get_platform()
    for file in files:
        # Strict filename guard: only ``<pid>.json`` is a candidate (see module docstring).
        if not _PID_FILE_RE.match(file):
            continue
        try:
            pid = int(file[:-5], 10)
        except ValueError:
            continue
        if pid == own_pid:
            count += 1
            continue
        if is_process_running(pid):
            count += 1
        elif platform != "wsl":
            # Stale file from a crashed session — sweep it. Skip on WSL: a Windows PID isn't
            # probeable from WSL and we'd falsely delete a live session's file. This is just
            # telemetry so conservative undercount is acceptable.
            await _safe_unlink(_osp.join(directory, file))
    return count


async def _safe_unlink(path: str) -> None:
    """``void unlink(path).catch(() => {})`` — swallow any error."""
    with contextlib.suppress(Exception):
        await asyncio.to_thread(os.unlink, path)


def _now_ms() -> int:
    """``Date.now()`` — milliseconds since the epoch."""
    import time

    return int(time.time() * 1000)
