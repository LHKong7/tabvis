"""Windows path / git-bash helpers

Surface:

* :func:`set_shell_if_windows` — on Windows, point ``SHELL`` at the git-bash ``bash.exe``.
* :func:`find_git_bash_path` — locate git-bash's ``bash.exe`` (memoized), exiting the process if
  not found. Honors ``TABVIS_GIT_BASH_PATH``.
* :func:`windows_path_to_posix_path` / :func:`posix_path_to_windows_path` — pure-string path
  conversions (UNC, drive-letter, ``/cygdrive/``, ``/c/`` MSYS2 forms), each LRU-memoized (500).

Behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``getPlatform() === 'windows'`` → :func:`tabvis.utils.platform.get_platform`. On macos/wsl (the
  locked supported platforms) :func:`set_shell_if_windows` is a no-op, exactly as in TS.
- ``execSync_DEPRECATED`` → :func:`tabvis.utils.exec_sync_wrapper.exec_sync_deprecated` (the same
  wrapped sync shell-out the TS uses; ``dir``/``where.exe`` probes are Windows-only).
- ``getCwd`` → :func:`tabvis.utils.cwd.get_cwd`. ``logForDebugging`` →
  :func:`tabvis.utils.debug.log_for_debugging`. ``path``/``path/win32`` → :mod:`os.path` /
  :mod:`ntpath`.
- lodash ``memoize`` (zero/one-arg) → the richer :func:`tabvis.utils.memoize.memoize_with_lru`
  for the converters (the TS uses ``memoizeWithLRU`` there, capacity 500) and a single-slot
  ``memoize_with_lru`` for the zero-arg :func:`find_git_bash_path` (lodash ``memoize`` over a
  zero-arg fn caches its first result — a 1-key LRU matches).
- ``process.exit(1)`` → :func:`sys.exit` (raises ``SystemExit``), matching the TS hard-exit.
- The conversion functions return plain ``str`` paths — no wire-key dicts here.

Casing: Python identifiers are snake_case; the module-level converters keep their TS names
(snake-cased).
"""

from __future__ import annotations

import ntpath
import os
import sys

from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.exec_sync_wrapper import exec_sync_deprecated
from tabvis.utils.memoize import memoize_with_lru
from tabvis.utils.platform import get_platform


def _check_path_exists(path: str) -> bool:
    """Whether a file or directory exists on Windows, via the ``dir`` command."""
    try:
        exec_sync_deprecated(f'dir "{path}"', {"stdio": "pipe"})
        return True
    except Exception:  # noqa: BLE001 - any failure (incl. non-zero exit) means "not found"
        return False


def _find_executable(executable: str) -> str | None:
    """Find an executable using ``where.exe`` on Windows; ``None`` if not found.

    For ``git`` it probes common install locations first, then falls back to ``where.exe`` and
    filters out any candidate in the current directory (to avoid running a malicious
    git.bat/cmd/exe planted in cwd).
    """
    if executable == "git":
        default_locations = [
            # Check 64-bit before 32-bit.
            "C:\\Program Files\\Git\\cmd\\git.exe",
            "C:\\Program Files (x86)\\Git\\cmd\\git.exe",
        ]
        for location in default_locations:
            if _check_path_exists(location):
                return location

    try:
        result = exec_sync_deprecated(
            f"where.exe {executable}",
            {"stdio": "pipe", "encoding": "utf8"},
        )
        result = result.strip() if isinstance(result, str) else result.decode("utf-8").strip()

        # SECURITY: filter out results from the current directory to prevent executing a
        # malicious git.bat/cmd/exe planted there.
        paths = [p for p in result.split("\r\n") if p]
        cwd = get_cwd().lower()

        for candidate_path in paths:
            normalized_path = ntpath.realpath(candidate_path).lower()
            path_dir = ntpath.dirname(normalized_path).lower()
            if path_dir == cwd or normalized_path.startswith(cwd + ntpath.sep):
                log_for_debugging(
                    "Skipping potentially malicious executable in current directory: "
                    f"{candidate_path}"
                )
                continue
            return candidate_path
        return None
    except Exception:  # noqa: BLE001 - mirror the TS catch-all
        return None


def set_shell_if_windows() -> None:
    """On Windows, set ``SHELL`` to the git-bash path (used by BashTool / Shell for user commands).

    ``COMSPEC`` is left unchanged for system process execution. No-op on non-Windows platforms.
    """
    if get_platform() == "windows":
        git_bash_path = find_git_bash_path()
        os.environ["SHELL"] = git_bash_path
        log_for_debugging(f'Using bash path: "{git_bash_path}"')


def _find_git_bash_path_impl() -> str:
    configured = os.environ.get("TABVIS_GIT_BASH_PATH")
    if configured:
        if _check_path_exists(configured):
            return configured
        print(
            f'Tabvis was unable to find TABVIS_GIT_BASH_PATH path "{configured}"',
            file=sys.stderr,
        )
        sys.exit(1)

    git_path = _find_executable("git")
    if git_path:
        bash_path = ntpath.join(git_path, "..", "..", "bin", "bash.exe")
        if _check_path_exists(bash_path):
            return bash_path

    print(
        "Tabvis on Windows requires git-bash (https://git-scm.com/downloads/win). If installed but "
        "not in PATH, set environment variable pointing to your bash.exe, similar to: "
        "TABVIS_GIT_BASH_PATH=C:\\Program Files\\Git\\bin\\bash.exe",
        file=sys.stderr,
    )
    sys.exit(1)


# lodash ``memoize`` over a zero-arg fn caches its first result; a single-slot LRU matches.
find_git_bash_path = memoize_with_lru(_find_git_bash_path_impl, lambda: "_", 1)


def _windows_path_to_posix_path(windows_path: str) -> str:
    """Convert a Windows path to a POSIX path using pure string ops."""
    # UNC paths: \\server\share -> //server/share.
    if windows_path.startswith("\\\\"):
        return windows_path.replace("\\", "/")
    # Drive-letter paths: C:\Users\foo -> /c/Users/foo.
    if len(windows_path) >= 3 and windows_path[0].isalpha() and windows_path[1] == ":" and windows_path[2] in "/\\":
        drive_letter = windows_path[0].lower()
        return "/" + drive_letter + windows_path[2:].replace("\\", "/")
    # Already POSIX or relative — just flip slashes.
    return windows_path.replace("\\", "/")


windows_path_to_posix_path = memoize_with_lru(
    _windows_path_to_posix_path, lambda p: p, 500
)


def _posix_path_to_windows_path(posix_path: str) -> str:
    """Convert a POSIX path to a Windows path using pure string ops."""
    # UNC paths: //server/share -> \\server\share.
    if posix_path.startswith("//"):
        return posix_path.replace("/", "\\")
    # /cygdrive/c/... format.
    if (
        posix_path.startswith("/cygdrive/")
        and len(posix_path) > len("/cygdrive/")
        and posix_path[len("/cygdrive/")].isalpha()
        and (len(posix_path) == len("/cygdrive/") + 1 or posix_path[len("/cygdrive/") + 1] == "/")
    ):
        drive_letter = posix_path[len("/cygdrive/")].upper()
        rest = posix_path[len("/cygdrive/" + posix_path[len("/cygdrive/")]) :]
        return drive_letter + ":" + (rest or "\\").replace("/", "\\")
    # /c/... format (MSYS2 / Git Bash).
    if (
        len(posix_path) >= 2
        and posix_path[0] == "/"
        and posix_path[1].isalpha()
        and (len(posix_path) == 2 or posix_path[2] == "/")
    ):
        drive_letter = posix_path[1].upper()
        rest = posix_path[2:]
        return drive_letter + ":" + (rest or "\\").replace("/", "\\")
    # Already Windows or relative — just flip slashes.
    return posix_path.replace("/", "\\")


posix_path_to_windows_path = memoize_with_lru(
    _posix_path_to_windows_path, lambda p: p, 500
)
