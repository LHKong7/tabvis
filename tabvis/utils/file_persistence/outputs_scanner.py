"""Outputs directory scanner for file persistence.

Scan persistence output directories and classify their files.

This module provides utilities to:
- Detect the environment kind from environment variables
- Find modified files by comparing file mtimes against the turn start time

The TS scanner uses ``fs.readdir(outputsDir, {withFileTypes:true, recursive:true})`` plus a
parallelized ``fs.lstat`` pass. Python walks the tree with :func:`os.walk` (``followlinks=False``
so symlinked directories are not descended — parity with skipping symlinks for security) and uses
:func:`os.lstat` to read mtimes without following symlinks. Regular files only; symlinks are
skipped both at discovery and after the (re-)stat (TOCTOU race guard, mirroring the TS double
check).

Times are compared in **milliseconds**: :class:`TurnStartTime` is epoch-ms (TS ``Date.now()`` /
``stat.mtimeMs``), and ``os.lstat().st_mtime`` (seconds) is scaled by 1000.
"""

from __future__ import annotations

import os
from typing import Literal

from tabvis.utils.debug import log_for_debugging

from .types import TurnStartTime

# Inlined from the removed teleport package: environment-kind wire values.
EnvironmentKind = Literal["byoc", "provider_cloud"]
ENVIRONMENT_KINDS: frozenset[str] = frozenset({"byoc", "provider_cloud"})


def log_debug(message: str) -> None:
    """Shared debug logger for file-persistence modules."""
    log_for_debugging(f"[file-persistence] {message}")


def get_environment_kind() -> EnvironmentKind | None:
    """Return the environment kind from ``TABVIS_ENVIRONMENT_KIND``.

    Returns ``None`` if unset or not a recognized value (``'byoc'`` / ``'provider_cloud'``).
    """
    kind = os.environ.get("TABVIS_ENVIRONMENT_KIND")
    if kind in ENVIRONMENT_KINDS:
        # ``kind`` is a validated member of the Literal; cast is purely for static typing.
        return kind  # type: ignore[return-value]
    return None


def find_modified_files(
    turn_start_time: TurnStartTime,
    outputs_dir: str,
) -> list[str]:
    """Find files modified since the turn started.

    Returns paths of regular files with ``mtime >= turn_start_time`` (both in epoch-ms).
    Symlinks are skipped for security. Missing/inaccessible directories yield ``[]``.

    :param turn_start_time: Epoch-ms timestamp when the turn started.
    :param outputs_dir: Directory to scan for modified files.
    """
    # Discover regular files (skip symlinks; do not descend into symlinked directories).
    file_paths: list[str] = []
    try:
        for root, _dirs, files in os.walk(outputs_dir, followlinks=False):
            for name in files:
                file_path = os.path.join(root, name)
                # Skip symlinks at discovery time (security — parity with entry.isSymbolicLink()).
                if os.path.islink(file_path):
                    continue
                file_paths.append(file_path)
    except OSError:
        # Directory doesn't exist or is not accessible.
        return []

    if not file_paths:
        log_debug("No files found in outputs directory")
        return []

    # Stat each file; tolerate races (deleted / turned-symlink between walk and stat).
    modified_files: list[str] = []
    for file_path in file_paths:
        try:
            stat = os.lstat(file_path)
        except OSError:
            # File may have been deleted between walk and stat.
            continue
        # Skip if it became a symlink between walk and stat (race condition).
        if os.path.islink(file_path):
            continue
        mtime_ms = stat.st_mtime * 1000
        if mtime_ms >= turn_start_time:
            modified_files.append(file_path)

    log_debug(
        f"Found {len(modified_files)} modified files since turn start "
        f"(scanned {len(file_paths)} total)"
    )

    return modified_files


__all__ = ["find_modified_files", "get_environment_kind", "log_debug"]
