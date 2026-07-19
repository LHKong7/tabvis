"""Platform / OS detection

Detects the running platform (macos / windows / wsl / linux / unknown), the WSL version, Linux
distro info, and the VCS systems present in a directory. Reads ``/proc/version`` /
``/etc/os-release`` through the swappable :func:`tabvis.utils.fs_operations.get_fs_implementation`
so the detection can be mocked.

lodash ``memoize`` → :func:`functools.lru_cache` for the sync zero-arg getters; the async
``getLinuxDistroInfo`` uses a hand-rolled single-slot memo (``lru_cache`` doesn't wrap
coroutines). The memos cache the FIRST result, matching the TS module-scope ``memoize`` — call
:func:`reset_platform_cache_for_tests` to re-detect after monkeypatching ``sys.platform``.

``process.platform`` → :data:`sys.platform` (``darwin`` / ``win32`` / ``linux``), matching Node's
values verbatim. Supported platforms are locked to macos/wsl per ``docs/SPINE_CONTRACTS.md``.
"""

from __future__ import annotations

import asyncio
import functools
import os
import platform as _platform
import re
import sys
from typing import Literal

from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.log import log_error

Platform = Literal["macos", "windows", "wsl", "linux", "unknown"]

SUPPORTED_PLATFORMS: list[Platform] = ["macos", "wsl"]


@functools.lru_cache(maxsize=1)
def get_platform() -> Platform:
    try:
        if sys.platform == "darwin":
            return "macos"

        if sys.platform == "win32":
            return "windows"

        if sys.platform == "linux":
            # Check if running in WSL (Windows Subsystem for Linux).
            try:
                proc_version = get_fs_implementation().read_file_sync(
                    "/proc/version", {"encoding": "utf8"}
                )
                lowered = proc_version.lower()
                if "microsoft" in lowered or "wsl" in lowered:
                    return "wsl"
            except Exception as error:  # noqa: BLE001
                # Error reading /proc/version, assume regular Linux.
                log_error(error)

            # Regular Linux.
            return "linux"

        # Unknown platform.
        return "unknown"
    except Exception as error:  # noqa: BLE001
        log_error(error)
        return "unknown"


@functools.lru_cache(maxsize=1)
def get_wsl_version() -> str | None:
    # Only check for WSL on Linux systems.
    if sys.platform != "linux":
        return None
    try:
        proc_version = get_fs_implementation().read_file_sync(
            "/proc/version", {"encoding": "utf8"}
        )

        # First check for explicit WSL version markers (e.g. "WSL2", "WSL3", …).
        wsl_version_match = re.search(r"WSL(\d+)", proc_version, re.IGNORECASE)
        if wsl_version_match and wsl_version_match.group(1):
            return wsl_version_match.group(1)

        # No explicit WSL version but contains Microsoft → assume WSL1. Handles the original
        # WSL1 format: "4.4.0-19041-Microsoft".
        if "microsoft" in proc_version.lower():
            return "1"

        # Not WSL or unable to determine version.
        return None
    except Exception as error:  # noqa: BLE001
        log_error(error)
        return None


class LinuxDistroInfo(dict):
    """Result of :func:`get_linux_distro_info`: ``linuxDistroId`` / ``linuxDistroVersion`` /
    ``linuxKernel``.

    Wire keys kept camelCase (per ``docs/SPINE_CONTRACTS.md``): this round-trips into env-info /
    diagnostics payloads. A plain ``dict`` subclass so callers can read the exact keys.
    """


# Single-slot async memo (lodash memoize over a zero-arg async fn caches the first promise).
_LINUX_DISTRO_INFO_CACHED = False
_LINUX_DISTRO_INFO_VALUE: LinuxDistroInfo | None = None


async def get_linux_distro_info() -> LinuxDistroInfo | None:
    global _LINUX_DISTRO_INFO_CACHED, _LINUX_DISTRO_INFO_VALUE
    if _LINUX_DISTRO_INFO_CACHED:
        return _LINUX_DISTRO_INFO_VALUE

    result = await _compute_linux_distro_info()
    _LINUX_DISTRO_INFO_VALUE = result
    _LINUX_DISTRO_INFO_CACHED = True
    return result


async def _compute_linux_distro_info() -> LinuxDistroInfo | None:
    if sys.platform != "linux":
        return None

    result = LinuxDistroInfo()
    result["linuxKernel"] = _platform.release()

    try:
        content = await get_fs_implementation().read_file(
            "/etc/os-release", {"encoding": "utf8"}
        )
        for line in content.split("\n"):
            match = re.match(r"^(ID|VERSION_ID)=(.*)$", line)
            if match and match.group(1) and match.group(2):
                value = re.sub(r'^"|"$', "", match.group(2))
                if match.group(1) == "ID":
                    result["linuxDistroId"] = value
                else:
                    result["linuxDistroVersion"] = value
    except Exception:  # noqa: BLE001
        # /etc/os-release may not exist on all Linux systems.
        pass

    return result


_VCS_MARKERS: list[tuple[str, str]] = [
    (".git", "git"),
    (".hg", "mercurial"),
    (".svn", "svn"),
    (".p4config", "perforce"),
    ("$tf", "tfs"),
    (".tfvc", "tfs"),
    (".jj", "jujutsu"),
    (".sl", "sapling"),
]


async def detect_vcs(directory: str | None = None) -> list[str]:
    detected: set[str] = set()

    # Check for Perforce via env var.
    if os.environ.get("P4PORT"):
        detected.add("perforce")

    try:
        target_dir = directory if directory is not None else get_fs_implementation().cwd()
        # TS reads the directory listing directly via `fs/promises.readdir` (string names),
        # not through the fs implementation — mirror that with a plain listdir off-thread.
        entries = set(await asyncio.to_thread(os.listdir, target_dir))
        for marker, vcs in _VCS_MARKERS:
            if marker in entries:
                detected.add(vcs)
    except Exception:  # noqa: BLE001
        # Directory may not be readable.
        pass

    return list(detected)


def reset_platform_cache_for_tests() -> None:
    """Clear the memoized platform/WSL/distro results so detection re-runs. Tests only."""
    global _LINUX_DISTRO_INFO_CACHED, _LINUX_DISTRO_INFO_VALUE
    get_platform.cache_clear()
    get_wsl_version.cache_clear()
    _LINUX_DISTRO_INFO_CACHED = False
    _LINUX_DISTRO_INFO_VALUE = None
