"""Non-blocking MDM subprocess reads

Fires the MDM subprocess reads (plutil on macOS, ``reg query`` on Windows) without blocking, and
hands the raw stdout to the (not-yet-implemented) ``mdm/settings`` consumer. Minimal imports — only
``asyncio`` / ``os`` / ``sys`` / stdlib subprocess + :mod:`tabvis.utils.settings.mdm.constants`
(which only imports ``os``/``getpass``).

Two usage patterns (mirroring the TS):
1. Startup: :func:`start_mdm_raw_read` fires early; results consumed later via
   :func:`get_mdm_raw_read_promise`.
2. Poll/fallback: :func:`fire_raw_read` creates a fresh read on demand.

``process.platform`` -> :data:`sys.platform` (``darwin`` / ``win32`` / ``linux``). The Node
``execFile`` (callback, never throws) -> :func:`asyncio.create_subprocess_exec`; a non-zero exit /
spawn failure / timeout maps to ``code != 0`` with the captured (possibly empty) stdout, matching
``err ? 1 : 0``.

Casing: Python identifiers snake_case; :class:`RawReadResult` keeps the TS wire-ish field names
(``plistStdouts``/``hklmStdout``/``hkcuStdout``/``stdout``/``label``) so the consumer reads them
verbatim.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass

from .constants import (
    MDM_SUBPROCESS_TIMEOUT_MS,
    PLUTIL_ARGS_PREFIX,
    PLUTIL_PATH,
    WINDOWS_REGISTRY_KEY_PATH_HKCU,
    WINDOWS_REGISTRY_KEY_PATH_HKLM,
    WINDOWS_REGISTRY_VALUE_NAME,
    get_macos_plist_paths,
)


@dataclass
class RawReadResult:
    """Raw stdout captured from the MDM subprocess reads.

    ``plist_stdouts`` mirrors ``plistStdouts``: ``None`` off-darwin, else a list of
    ``{"stdout", "label"}`` dicts (the winning source, or ``[]`` when none read). ``hklm_stdout`` /
    ``hkcu_stdout`` mirror ``hklmStdout`` / ``hkcuStdout`` (``None`` off-win32).
    """

    plist_stdouts: list[dict[str, str]] | None
    hklm_stdout: str | None
    hkcu_stdout: str | None


_raw_read_promise: asyncio.Future[RawReadResult] | None = None


async def _exec_file(cmd: str, args: list[str]) -> dict[str, object]:
    """Run ``cmd args`` capturing utf-8 stdout, never raising.

    Returns ``{"stdout": str, "code": 0|1}`` — ``code`` is ``0`` on a clean exit, ``1`` on a
    non-zero exit / spawn failure / timeout (matching the TS ``err ? 1 : 0``).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError):
        return {"stdout": "", "code": 1}

    timeout_s = MDM_SUBPROCESS_TIMEOUT_MS / 1000
    try:
        stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"stdout": "", "code": 1}

    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    code = 0 if proc.returncode == 0 else 1
    return {"stdout": stdout, "code": code}


async def _read_plist(path: str, label: str) -> dict[str, object]:
    """Fast-path skip + plutil read for one macOS plist (helper for the darwin branch)."""
    # Fast-path: skip the plutil subprocess if the plist file does not exist. Spawning plutil
    # takes ~5ms even for an immediate ENOENT, and non-MDM machines never have these files.
    if not os.path.exists(path):
        return {"stdout": "", "label": label, "ok": False}
    result = await _exec_file(PLUTIL_PATH, [*PLUTIL_ARGS_PREFIX, path])
    ok = result["code"] == 0 and bool(result["stdout"])
    return {"stdout": result["stdout"], "label": label, "ok": ok}


async def fire_raw_read() -> RawReadResult:
    """Fire fresh MDM subprocess reads and return the raw stdout.

    - macOS: spawns plutil for each plist path in parallel, picks the first winner (priority order).
    - Windows: spawns ``reg query`` for HKLM and HKCU in parallel.
    - Linux/other: returns empty (no MDM equivalent).
    """
    if sys.platform == "darwin":
        plist_paths = get_macos_plist_paths()

        all_results = await asyncio.gather(
            *(_read_plist(entry["path"], entry["label"]) for entry in plist_paths)
        )

        # First source wins (list is in priority order).
        winner = next((r for r in all_results if r["ok"]), None)
        return RawReadResult(
            plist_stdouts=(
                [{"stdout": str(winner["stdout"]), "label": str(winner["label"])}]
                if winner
                else []
            ),
            hklm_stdout=None,
            hkcu_stdout=None,
        )

    if sys.platform == "win32":
        hklm, hkcu = await asyncio.gather(
            _exec_file(
                "reg",
                ["query", WINDOWS_REGISTRY_KEY_PATH_HKLM, "/v", WINDOWS_REGISTRY_VALUE_NAME],
            ),
            _exec_file(
                "reg",
                ["query", WINDOWS_REGISTRY_KEY_PATH_HKCU, "/v", WINDOWS_REGISTRY_VALUE_NAME],
            ),
        )
        return RawReadResult(
            plist_stdouts=None,
            hklm_stdout=str(hklm["stdout"]) if hklm["code"] == 0 else None,
            hkcu_stdout=str(hkcu["stdout"]) if hkcu["code"] == 0 else None,
        )

    return RawReadResult(plist_stdouts=None, hklm_stdout=None, hkcu_stdout=None)


def start_mdm_raw_read() -> None:
    """Fire the raw reads once for startup.

    Idempotent. Schedules :func:`fire_raw_read` as a task on the running event loop so the result
    is consumed later via :func:`get_mdm_raw_read_promise`. Requires a running loop (the TS variant
    fires at module evaluation under Node's always-present loop).
    """
    global _raw_read_promise
    if _raw_read_promise is not None:
        return
    _raw_read_promise = asyncio.ensure_future(fire_raw_read())


def get_mdm_raw_read_promise() -> asyncio.Future[RawReadResult] | None:
    """The startup future, or ``None`` if :func:`start_mdm_raw_read` was not called.

    Return the mdm raw read promise.
    """
    return _raw_read_promise


def reset_mdm_raw_read_for_tests() -> None:
    """Clear the startup promise (test-only; not in the TS surface)."""
    global _raw_read_promise
    _raw_read_promise = None
