"""File write/read/path helpers

Scope (per implementation plan): only the surface the three file tools import from
``utils/file.js``:

* ``FileReadTool`` → ``add_line_numbers``, ``FILE_NOT_FOUND_CWD_NOTE``,
  ``find_similar_file``, ``get_file_modification_time_async``,
  ``suggest_path_under_cwd``.
* ``FileWriteTool`` → ``get_file_modification_time``, ``write_text_content``.
* ``FileEditTool`` → ``FILE_NOT_FOUND_CWD_NOTE``, ``find_similar_file``,
  ``get_display_path``, ``get_file_modification_time``,
  ``suggest_path_under_cwd``, ``write_text_content``.

The atomic-write core (``write_file_sync_and_flush``) mirrors the TS
``writeFileSyncAndFlush_DEPRECATED``: symlink-aware target resolution, atomic
temp-file write + fsync flush + permission preservation + ``rename``, falling
back to a direct flushed write on failure. ``write_text_content`` applies the
same CRLF normalization the TS does before delegating.

Casing: Python identifiers are snake_case; ``FILE_NOT_FOUND_CWD_NOTE`` keeps the
exact user-facing string (it is matched verbatim by UI renderers).
"""

from __future__ import annotations

import math
import os
import unicodedata
from pathlib import Path

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import is_enoent
from tabvis.utils.log import log_error

# 0.25MB in bytes — kept for parity with the TS export (read-size guard).
MAX_OUTPUT_SIZE = int(0.25 * 1024 * 1024)

# Marker included in file-not-found error messages that carry a cwd note. UI
# renderers check for this verbatim to show a short "File not found" message.
FILE_NOT_FOUND_CWD_NOTE = "Note: your current working directory is"

# Line-ending discriminant (TS ``LineEndingType = 'CRLF' | 'LF'``).
LineEndingType = str  # 'CRLF' | 'LF'


# --- cwd / path stubs ---------------------------------------------------------
# once those modules land. These minimal stubs reproduce the TS semantics the
# three file tools depend on (cwd resolution + ``~`` expansion + NFC normalize).


def _get_cwd() -> str:
    """Minimal stand-in for ``getCwd()`` (resolved current working directory)."""
    try:
        # Match the TS oracle which stores a realpath-resolved cwd.
        return os.path.realpath(os.getcwd())
    except OSError:
        return os.getcwd()


def _expand_path(path: str, base_dir: str | None = None) -> str:
    """Expand ``~`` and resolve ``path`` to a normalized (NFC) absolute path."""
    if "\0" in path:
        raise ValueError("Path contains null bytes")
    actual_base = base_dir if base_dir is not None else _get_cwd()
    trimmed = path.strip()
    if not trimmed:
        return unicodedata.normalize("NFC", os.path.normpath(actual_base))
    home = os.path.expanduser("~")
    if trimmed == "~":
        return unicodedata.normalize("NFC", home)
    if trimmed.startswith("~/"):
        return unicodedata.normalize("NFC", os.path.join(home, trimmed[2:]))
    if os.path.isabs(trimmed):
        return unicodedata.normalize("NFC", os.path.normpath(trimmed))
    return unicodedata.normalize("NFC", os.path.normpath(os.path.join(actual_base, trimmed)))


# --- modification time --------------------------------------------------------


def get_file_modification_time(file_path: str) -> int:
    """Floored mtime in ms.

    ``Math.floor`` (TS) ensures consistent timestamp comparisons across file
    operations, reducing false positives from sub-millisecond precision changes.
    """
    return math.floor(os.stat(file_path).st_mtime * 1000)


async def get_file_modification_time_async(file_path: str) -> int:
    """Async variant of :func:`get_file_modification_time` (same floor semantics).

    The TS uses an async fs ``stat`` to avoid the slow-operation indicator on
    network/slow disks; Python's ``os.stat`` is fast and non-blocking enough for
    the skeleton, so this simply awaits-free delegates.
    """
    return get_file_modification_time(file_path)


# --- line-number formatting ---------------------------------------------------


def is_compact_line_prefix_enabled() -> bool:
    """Whether to use the compact ``N\\t`` line-number prefix instead of ``N→``.

    Always uses the compact format (client-side only).
    """
    return True


def add_line_numbers(content: str, start_line: int) -> str:
    """Add ``cat -n`` style line numbers to ``content`` (1-indexed ``start_line``)."""
    if not content:
        return ""

    # Split on \r?\n to match the TS regex (handles CRLF + LF).
    lines = content.replace("\r\n", "\n").split("\n")

    if is_compact_line_prefix_enabled():
        return "\n".join(f"{index + start_line}\t{line}" for index, line in enumerate(lines))

    out: list[str] = []
    for index, line in enumerate(lines):
        num_str = str(index + start_line)
        if len(num_str) >= 6:
            out.append(f"{num_str}→{line}")
        else:
            out.append(f"{num_str.rjust(6, ' ')}→{line}")
    return "\n".join(out)


def strip_line_number_prefix(line: str) -> str:
    """Inverse of :func:`add_line_numbers` — strip the ``N→`` / ``N\\t`` prefix.

    Co-located so format changes here and in :func:`add_line_numbers` stay in
    sync.
    """
    import re

    match = re.match(r"^\s*\d+[→\t](.*)$", line, re.DOTALL)
    return match.group(1) if match else line


# --- path display -------------------------------------------------------------


def get_absolute_and_relative_paths(
    path: str | None,
) -> tuple[str | None, str | None]:
    """Return ``(absolute_path, relative_path)`` for ``path`` (relative to cwd)."""
    absolute_path = _expand_path(path) if path else None
    relative_path = os.path.relpath(absolute_path, _get_cwd()) if absolute_path else None
    return absolute_path, relative_path


def get_display_path(file_path: str) -> str:
    """Shortest unambiguous display path: relative-to-cwd, ``~`` home, else absolute."""
    _absolute, relative_path = get_absolute_and_relative_paths(file_path)
    if relative_path and not relative_path.startswith(".."):
        return relative_path

    home_dir = os.path.expanduser("~")
    if file_path.startswith(home_dir + os.sep):
        return "~" + file_path[len(home_dir) :]

    return file_path


# --- similar-file discovery ---------------------------------------------------


def find_similar_file(file_path: str) -> str | None:
    """Find a file with the same base name but a different extension in the same dir.

    Returns just the filename (basename) of the first match, or ``None``.
    """
    try:
        p = Path(file_path)
        directory = str(p.parent)
        file_base_name = p.stem

        names = os.listdir(directory)
        for name in names:
            cand = Path(name)
            if cand.stem == file_base_name and os.path.join(directory, name) != file_path:
                return name
        return None
    except OSError as error:
        # Missing dir (ENOENT) is expected; for other errors log and return None.
        if not is_enoent(error):
            log_error(error)
        return None


# --- corrected-path suggestion ------------------------------------------------


async def suggest_path_under_cwd(requested_path: str) -> str | None:
    """Suggest a corrected path under cwd for a not-found absolute path.

    Detects the "dropped repo folder" pattern where the model builds an absolute
    path missing the repo directory component. Returns the corrected path if it
    exists under cwd, else ``None``.
    """
    cwd = _get_cwd()
    cwd_parent = os.path.dirname(cwd)
    sep = os.sep

    # Resolve symlinks in the requested path's parent (e.g. /tmp -> /private/tmp
    # on macOS) so the prefix comparison works against the realpath-resolved cwd.
    resolved_path = requested_path
    try:
        resolved_dir = os.path.realpath(os.path.dirname(requested_path))
        resolved_path = os.path.join(resolved_dir, os.path.basename(requested_path))
    except OSError:
        # Parent directory doesn't exist, use the original path.
        pass

    # Only check if the requested path is under cwd's parent but not under cwd.
    # When cwd_parent is the root ('/'), use it directly to avoid a '//' prefix.
    cwd_parent_prefix = sep if cwd_parent == sep else cwd_parent + sep
    if (
        not resolved_path.startswith(cwd_parent_prefix)
        or resolved_path.startswith(cwd + sep)
        or resolved_path == cwd
    ):
        return None

    rel_from_parent = os.path.relpath(resolved_path, cwd_parent)
    corrected_path = os.path.join(cwd, rel_from_parent)
    try:
        os.stat(corrected_path)
        return corrected_path
    except OSError:
        return None


# --- text write ---------------------------------------------------------------


def write_text_content(
    file_path: str,
    content: str,
    encoding: str,
    endings: LineEndingType,
) -> None:
    """Write ``content`` to ``file_path``, applying CRLF normalization if requested."""
    to_write = content
    if endings == "CRLF":
        # Normalize any existing CRLF to LF first so a new_string that already
        # contains \r\n (raw model output) doesn't become \r\r\n after the join.
        to_write = "\n".join(content.replace("\r\n", "\n").split("\n")).replace("\n", "\r\n")

    write_file_sync_and_flush(file_path, to_write, encoding=encoding)


def write_file_sync_and_flush(
    file_path: str,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Atomic write + fsync flush, preserving symlinks and permissions.

    Resolve through a symlink to
    its target, write to a temp file (flushed to disk), preserve/apply the
    target's permissions, then ``rename`` atomically. On any failure, clean up
    the temp file and fall back to a direct flushed write.
    """
    # Resolve through a symlink to preserve the link for all users (write to the
    # target, keep the symlink itself). We deliberately do not canonicalize the
    # whole path so a non-symlink path is written in place.
    target_path = file_path
    try:
        link_target = os.readlink(file_path)
        target_path = (
            link_target
            if os.path.isabs(link_target)
            else os.path.normpath(os.path.join(os.path.dirname(file_path), link_target))
        )
        log_for_debugging(f"Writing through symlink: {file_path} -> {target_path}")
    except OSError:
        # ENOENT (doesn't exist) or EINVAL (not a symlink) — keep file_path.
        pass

    temp_path = f"{target_path}.tmp.{os.getpid()}.{_now_ms()}"

    # Single stat reused by both atomic and fallback paths.
    target_mode: int | None = None
    target_exists = False
    try:
        target_mode = os.stat(target_path).st_mode
        target_exists = True
        log_for_debugging(f"Preserving file permissions: {oct(target_mode)}")
    except OSError as e:
        if not is_enoent(e):
            raise
        if mode is not None:
            target_mode = mode
            log_for_debugging(f"Setting permissions for new file: {oct(target_mode)}")

    try:
        log_for_debugging(f"Writing to temp file: {temp_path}")
        _write_flushed(temp_path, content, encoding)
        log_for_debugging(f"Temp file written successfully, size: {len(content)} bytes")

        # For new files, apply the requested mode; for existing files, restore
        # the original mode onto the temp file before the rename.
        if not target_exists and mode is not None:
            os.chmod(temp_path, mode)
        elif target_exists and target_mode is not None:
            os.chmod(temp_path, target_mode)
            log_for_debugging("Applied original permissions to temp file")

        log_for_debugging(f"Renaming {temp_path} to {target_path}")
        os.replace(temp_path, target_path)
        log_for_debugging(f"File {target_path} written atomically")
    except OSError as atomic_error:
        log_for_debugging(f"Failed to write file atomically: {atomic_error}")

        try:
            log_for_debugging(f"Cleaning up temp file: {temp_path}")
            os.unlink(temp_path)
        except OSError as cleanup_error:
            log_for_debugging(f"Failed to clean up temp file: {cleanup_error}")

        log_for_debugging(f"Falling back to non-atomic write for {target_path}")
        try:
            _write_flushed(target_path, content, encoding)
            if not target_exists and mode is not None:
                os.chmod(target_path, mode)
            log_for_debugging(
                f"File {target_path} written successfully with non-atomic fallback"
            )
        except OSError as fallback_error:
            log_for_debugging(f"Non-atomic write also failed: {fallback_error}")
            raise


def _write_flushed(path: str, content: str, encoding: str) -> None:
    """Write ``content`` to ``path`` and fsync it to disk (the TS ``flush: true``)."""
    data = content.encode(encoding)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)
