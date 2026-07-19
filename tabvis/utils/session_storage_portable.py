"""Portable session storage utilities

Pure-stdlib — no internal dependencies on logging, experiments, or feature flags. Shared
(in the TS tree) between the CLI and the VS Code extension.

Casing: Python identifiers are snake_case; constants UPPER_CASE. The session-file metadata
shape (``LiteSessionFile``) is a plain dict whose keys (``mtime``/``size``/``head``/``tail``)
are the wire keys the TS callers read — preserved verbatim. The transcript-load result keeps
its ``boundaryStartOffset``/``postBoundaryBuf``/``hasPreservedSegment`` wire keys.

Faithful-behavior notes:
- ``sanitize_path`` mirrors the TS exactly: replace ``[^a-zA-Z0-9]`` with ``-``; for names over
  ``MAX_SANITIZED_LENGTH`` append ``abs(djb2_hash(name))`` in base-36. The Bun.hash fast path of
  the TS is not reachable under Python, so we always take the ``simpleHash`` (djb2) branch — the
  deterministic, on-disk-stable path. NOTE the local ``MAX_SANITIZED_LENGTH`` here (255-aware,
  this module's constant) is distinct from ``path.MAX_SANITIZED_LENGTH``; both are 200.
- ``unescape_json_string`` only allocates when a backslash is present; it parses ``"<raw>"`` as
  JSON to unescape (falling back to the raw text on failure), matching the TS.
- ``extract_json_string_field`` / ``extract_last_json_string_field`` scan raw text for
  ``"key":"value"`` (and ``"key": "value"``) without a full JSON parse — works on truncated
  lines. The escape handling (``\\`` skips two chars) matches the TS byte walk.
- ``extract_first_prompt_from_head`` reproduces the TS skip rules (tool_result / isMeta /
  isCompactSummary / command-name / bash-input / auto-generated XML), the 200-char truncation
  with a ``\\u2026`` ellipsis, and the command-name fallback.
- File I/O (``read_head_and_tail`` / ``read_session_lite``) returns the head and (for files
  larger than ``LITE_READ_BUF_SIZE``) the tail decoded as UTF-8; on any error returns the empty
  result / ``None`` exactly like the TS try/catch.
- ``read_transcript_for_load`` reproduces the TS chunked reader's *result* (strip
  attribution-snapshot lines keeping only the most recent — appended at EOF — and truncate the
  output at the last real ``compact_boundary`` without a ``preservedSegment``) with a faithful
  line-oriented single pass; the byte-level chunk/straddle bookkeeping is an I/O optimization
  whose observable output this matches.
- Path resolution (``resolve_session_file_path`` / ``find_project_dir`` / ``canonicalize_path``)
  uses ``asyncio.to_thread`` for the blocking ``os`` calls so the ``async`` surface is preserved;
  zero-byte files are treated as not-found, mirroring the TS ``s.size > 0`` guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from typing import Any

from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.get_worktree_paths_portable import get_worktree_paths_portable
from tabvis.utils.hash import djb2_hash

# --------------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------------

#: Size of the head/tail buffer for lite metadata reads.
LITE_READ_BUF_SIZE = 65536

#: Maximum length for a single filesystem path component (directory or file name). Most
#: filesystems (ext4, APFS, NTFS) limit individual components to 255 bytes; 200 leaves room for
#: the hash suffix and separator.
MAX_SANITIZED_LENGTH = 200

#: File size below which precompact filtering is skipped (large sessions almost always have
#: compact boundaries).
SKIP_PRECOMPACT_THRESHOLD = 5 * 1024 * 1024

# Chunk size for the forward transcript reader (1 MB).
_TRANSCRIPT_READ_CHUNK_SIZE = 1024 * 1024


# --------------------------------------------------------------------------------------------
# UUID validation
# --------------------------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def validate_uuid(maybe_uuid: Any) -> str | None:
    """Return ``maybe_uuid`` if it is a canonical UUID string, else ``None``.

    Validate the uuid.
    """
    if not isinstance(maybe_uuid, str):
        return None
    return maybe_uuid if _UUID_RE.match(maybe_uuid) else None


# --------------------------------------------------------------------------------------------
# JSON string field extraction — no full parse, works on truncated lines
# --------------------------------------------------------------------------------------------


def unescape_json_string(raw: str) -> str:
    """Unescape a JSON string value extracted as raw text.

    Only allocates a new string when escape sequences are present.
    ``unescapeJsonString``.
    """
    if "\\" not in raw:
        return raw
    try:
        return json.loads(f'"{raw}"')
    except (ValueError, TypeError):
        return raw


def extract_json_string_field(text: str, key: str) -> str | None:
    """Extract a simple JSON string field value from raw text without a full parse.

    Looks for ``"key":"value"`` or ``"key": "value"`` patterns. Returns the first match, or
    ``None`` if not found.
    """
    patterns = [f'"{key}":"', f'"{key}": "']
    for pattern in patterns:
        idx = text.find(pattern)
        if idx < 0:
            continue

        value_start = idx + len(pattern)
        i = value_start
        text_len = len(text)
        while i < text_len:
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == '"':
                return unescape_json_string(text[value_start:i])
            i += 1
    return None


def extract_last_json_string_field(text: str, key: str) -> str | None:
    """Like :func:`extract_json_string_field` but finds the LAST occurrence.

    Useful for fields that are appended (customTitle, tag, etc.).
    ``extractLastJsonStringField``.
    """
    patterns = [f'"{key}":"', f'"{key}": "']
    last_value: str | None = None
    text_len = len(text)
    for pattern in patterns:
        search_from = 0
        while True:
            idx = text.find(pattern, search_from)
            if idx < 0:
                break

            value_start = idx + len(pattern)
            i = value_start
            while i < text_len:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    last_value = unescape_json_string(text[value_start:i])
                    break
                i += 1
            search_from = i + 1
    return last_value


# --------------------------------------------------------------------------------------------
# First prompt extraction from head chunk
# --------------------------------------------------------------------------------------------

# Auto-generated / system messages skipped when looking for the first meaningful user prompt:
# anything starting with a lowercase XML-like tag, or a synthetic interrupt marker.
_SKIP_FIRST_PROMPT_PATTERN = re.compile(
    r"^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user[^\]]*\])"
)
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>")
_BASH_INPUT_RE = re.compile(r"<bash-input>(.*?)</bash-input>", re.DOTALL)


def extract_first_prompt_from_head(head: str) -> str:
    """Extract the first meaningful user prompt from a JSONL head chunk.

    Skips tool_result messages, isMeta, isCompactSummary, command-name messages, and
    auto-generated patterns (session hooks, tick, IDE metadata, etc.). Truncates to 200 chars.
    Extract the first prompt from head.
    """
    start = 0
    command_fallback = ""
    head_len = len(head)
    while start < head_len:
        newline_idx = head.find("\n", start)
        line = head[start:newline_idx] if newline_idx >= 0 else head[start:]
        start = newline_idx + 1 if newline_idx >= 0 else head_len

        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        if '"tool_result"' in line:
            continue
        if '"isMeta":true' in line or '"isMeta": true' in line:
            continue
        if '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
            continue

        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue

        message = entry.get("message")
        if not message or not isinstance(message, dict):
            continue

        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    texts.append(block["text"])

        for raw in texts:
            result = raw.replace("\n", " ").strip()
            if not result:
                continue

            # Skip slash-command messages but remember the first as a fallback.
            cmd_match = _COMMAND_NAME_RE.search(result)
            if cmd_match:
                if not command_fallback:
                    command_fallback = cmd_match.group(1)
                continue

            # Format bash input with a ``!`` prefix before the generic XML skip.
            bash_match = _BASH_INPUT_RE.search(result)
            if bash_match:
                return f"! {bash_match.group(1).strip()}"

            if _SKIP_FIRST_PROMPT_PATTERN.match(result):
                continue

            if len(result) > 200:
                result = result[:200].strip() + "…"
            return result

    if command_fallback:
        return command_fallback
    return ""


# --------------------------------------------------------------------------------------------
# File I/O — read head and tail of a file
# --------------------------------------------------------------------------------------------

LiteSessionFile = dict[str, Any]


def _read_head_tail_bytes(file_path: str, file_size: int) -> tuple[str, str]:
    """Synchronous head+tail read used by the async wrappers (parity with the TS fd logic)."""
    with open(file_path, "rb") as fh:  # noqa: PTH123 - low-level positional read
        head_bytes = fh.read(LITE_READ_BUF_SIZE)
        if len(head_bytes) == 0:
            return "", ""
        head = head_bytes.decode("utf-8", "replace")

        tail_offset = max(0, file_size - LITE_READ_BUF_SIZE)
        tail = head
        if tail_offset > 0:
            fh.seek(tail_offset)
            tail_bytes = fh.read(LITE_READ_BUF_SIZE)
            tail = tail_bytes.decode("utf-8", "replace")
        return head, tail


async def read_head_and_tail(
    file_path: str, file_size: int, buf: Any = None
) -> dict[str, str]:
    """Read the first and last ``LITE_READ_BUF_SIZE`` bytes of a file.

    For small files where head covers tail, ``tail == head``. Returns ``{"head": "", "tail":
    ""}`` on any error.
    parity — Python allocates per read).
    """
    try:
        head, tail = await asyncio.to_thread(
            _read_head_tail_bytes, file_path, file_size
        )
        return {"head": head, "tail": tail}
    except Exception:  # noqa: BLE001 - parity with the TS catch-all
        return {"head": "", "tail": ""}


def _read_session_lite_sync(file_path: str) -> LiteSessionFile | None:
    """Synchronous single-fd stat + head/tail read (parity with the TS ``readSessionLite``)."""
    with open(file_path, "rb") as fh:  # noqa: PTH123 - low-level positional read
        st = os.fstat(fh.fileno())
        head_bytes = fh.read(LITE_READ_BUF_SIZE)
        if len(head_bytes) == 0:
            return None
        head = head_bytes.decode("utf-8", "replace")
        tail_offset = max(0, st.st_size - LITE_READ_BUF_SIZE)
        tail = head
        if tail_offset > 0:
            fh.seek(tail_offset)
            tail_bytes = fh.read(LITE_READ_BUF_SIZE)
            tail = tail_bytes.decode("utf-8", "replace")
        return {
            "mtime": int(st.st_mtime * 1000),
            "size": st.st_size,
            "head": head,
            "tail": tail,
        }


async def read_session_lite(file_path: str) -> LiteSessionFile | None:
    """Open a single session file, stat it, and read head + tail in one fd.

    Returns ``None`` on any error.
    """
    try:
        return await asyncio.to_thread(_read_session_lite_sync, file_path)
    except Exception:  # noqa: BLE001 - parity with the TS catch-all
        return None


# --------------------------------------------------------------------------------------------
# Path sanitization
# --------------------------------------------------------------------------------------------


def _simple_hash(value: str) -> str:
    """``Abs(djb2_hash(value))`` in base-36."""
    return _to_base36(abs(djb2_hash(value)))


def _to_base36(n: int) -> str:
    """Encode a non-negative integer in base 36."""
    if n == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
    return out


def sanitize_path(name: str) -> str:
    """Make a string safe for use as a directory or file name.

    Replaces all non-alphanumeric characters with hyphens. For deeply nested paths that would
    exceed filesystem limits, truncates and appends a hash suffix for uniqueness. The Bun.hash
    fast path is unreachable under Python, so the djb2 ``simpleHash`` branch is always taken.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", name)
    if len(sanitized) <= MAX_SANITIZED_LENGTH:
        return sanitized
    return f"{sanitized[:MAX_SANITIZED_LENGTH]}-{_simple_hash(name)}"


# --------------------------------------------------------------------------------------------
# Project directory discovery
# --------------------------------------------------------------------------------------------


def get_projects_dir() -> str:
    """``<Config home>/projects``."""
    return os.path.join(get_tabvis_config_home_dir(), "projects")


def get_project_dir(project_dir: str) -> str:
    """``<Projects dir>/<sanitized(project_dir)>``."""
    return os.path.join(get_projects_dir(), sanitize_path(project_dir))


async def canonicalize_path(dir_path: str) -> str:
    """Resolve a directory path to its canonical form (realpath + NFC).
    ``canonicalizePath``.

    Falls back to NFC-only if realpath fails (e.g. the directory doesn't exist yet).
    """
    try:
        real = await asyncio.to_thread(os.path.realpath, dir_path, strict=True)
        return unicodedata.normalize("NFC", real)
    except OSError:
        return unicodedata.normalize("NFC", dir_path)


async def find_project_dir(project_path: str) -> str | None:
    """Find the project directory for a path, tolerating hash mismatches for long paths.

    The CLI uses Bun.hash while the SDK under Node uses simpleHash —
    for paths exceeding ``MAX_SANITIZED_LENGTH`` these produce different suffixes, so this falls
    back to prefix-based scanning when the exact match doesn't exist.
    """
    exact = get_project_dir(project_path)
    try:
        await asyncio.to_thread(os.listdir, exact)
        return exact
    except OSError:
        # Exact match failed — for short paths this means no sessions exist. For long paths, try
        # prefix matching to handle hash mismatches.
        sanitized = sanitize_path(project_path)
        if len(sanitized) <= MAX_SANITIZED_LENGTH:
            return None
        prefix = sanitized[:MAX_SANITIZED_LENGTH]
        projects_dir = get_projects_dir()
        try:
            names = await asyncio.to_thread(os.listdir, projects_dir)
        except OSError:
            return None
        for name in names:
            full = os.path.join(projects_dir, name)
            if name.startswith(prefix + "-") and await asyncio.to_thread(
                os.path.isdir, full
            ):
                return full
        return None


async def _stat_size(path: str) -> int | None:
    """Return the size of ``path`` or ``None`` if it can't be stat'd (ENOENT/EACCES/ENOTDIR)."""
    try:
        st = await asyncio.to_thread(os.stat, path)
        return st.st_size
    except OSError:
        return None


async def resolve_session_file_path(
    session_id: str,
    dir_path: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a sessionId to its on-disk JSONL file path.

    With ``dir_path``: canonicalize it, look in that project's directory (with
    :func:`find_project_dir` fallback), then fall back to sibling git worktrees. With no
    ``dir_path``: scan all project directories under ``<config home>/projects``.

    Zero-byte files are treated as not-found so callers keep searching past a truncated copy.
    Returns ``{"filePath", "projectPath", "fileSize"}`` or ``None``.
    """
    file_name = f"{session_id}.jsonl"

    if dir_path:
        canonical = await canonicalize_path(dir_path)
        project_dir = await find_project_dir(canonical)
        if project_dir:
            file_path = os.path.join(project_dir, file_name)
            size = await _stat_size(file_path)
            if size is not None and size > 0:
                return {
                    "filePath": file_path,
                    "projectPath": canonical,
                    "fileSize": size,
                }
        # Worktree fallback — sessions may live under a different worktree root.
        try:
            worktree_paths = await get_worktree_paths_portable(canonical)
        except Exception:  # noqa: BLE001 - parity with the TS catch-all
            worktree_paths = []
        for wt in worktree_paths:
            if wt == canonical:
                continue
            wt_project_dir = await find_project_dir(wt)
            if not wt_project_dir:
                continue
            file_path = os.path.join(wt_project_dir, file_name)
            size = await _stat_size(file_path)
            if size is not None and size > 0:
                return {
                    "filePath": file_path,
                    "projectPath": wt,
                    "fileSize": size,
                }
        return None

    # No dir — scan all project directories.
    projects_dir = get_projects_dir()
    try:
        names = await asyncio.to_thread(os.listdir, projects_dir)
    except OSError:
        return None
    for name in names:
        file_path = os.path.join(projects_dir, name, file_name)
        size = await _stat_size(file_path)
        if size is not None and size > 0:
            return {
                "filePath": file_path,
                "projectPath": None,
                "fileSize": size,
            }
    return None


# --------------------------------------------------------------------------------------------
# Compact-boundary chunked read
# --------------------------------------------------------------------------------------------

_ATTR_SNAP_PREFIX = b'{"type":"attribution-snapshot"'
_SYSTEM_PREFIX = b'{"type":"system"'
_COMPACT_BOUNDARY_MARKER = b'"compact_boundary"'
_BOUNDARY_SEARCH_BOUND = 256  # marker sits ~28 bytes in; 256 is slack


def _parse_boundary_line(line: str) -> dict[str, bool] | None:
    """Confirm a byte-matched line is a real compact_boundary and check preservedSegment.

    The marker can appear inside user content, so re-validate via
    JSON. Returns ``{"hasPreservedSegment": bool}`` or ``None``.
    """
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("type") != "system" or parsed.get("subtype") != "compact_boundary":
        return None
    compact_metadata = parsed.get("compactMetadata")
    preserved = bool(
        isinstance(compact_metadata, dict)
        and compact_metadata.get("preservedSegment")
    )
    return {"hasPreservedSegment": preserved}


async def read_transcript_for_load(
    file_path: str,
    file_size: int,
) -> dict[str, Any]:
    """Single forward read for the ``--resume`` load path.

    Strips attribution-snapshot lines (keeping only the most recent, re-appended at EOF) and
    truncates the output at the last real ``compact_boundary`` without a ``preservedSegment``
    (recording its file offset in ``boundaryStartOffset``). Returns
    ``{"boundaryStartOffset", "postBoundaryBuf" (bytes), "hasPreservedSegment"}``.

    Fidelity: the TS does this with chunked byte reads + chunk-seam straddle bookkeeping (an I/O
    optimization). This Python implementation reproduces the identical *observable result* via a faithful
    line-oriented pass; the per-line decisions (attr-snap strip, boundary marker pre-check +
    JSON re-validation, preservedSegment handling, last-snap reorder + LF insertion) match the
    TS branch-for-branch.
    """
    data = await asyncio.to_thread(_read_file_bytes, file_path, file_size)
    return _process_transcript_bytes(data)


def _read_file_bytes(file_path: str, file_size: int) -> bytes:
    with open(file_path, "rb") as fh:  # noqa: PTH123 - positional bulk read
        return fh.read(file_size)


def _process_transcript_bytes(data: bytes) -> dict[str, Any]:
    """Scan transcript bytes line by line while preserving boundary snapshots."""
    out = bytearray()
    boundary_start_offset = 0
    has_preserved_segment = False
    last_snap: bytes | None = None  # most-recent attr-snap, appended at EOF

    pos = 0  # file offset of the start of the current line
    data_len = len(data)
    while pos < data_len:
        nl = data.find(b"\n", pos)
        if nl == -1:
            line_end = data_len  # final line without a trailing newline
            line_bytes = data[pos:line_end]
            has_newline = False
        else:
            line_end = nl + 1
            line_bytes = data[pos:line_end]  # includes the trailing LF
            has_newline = True

        # Attribution-snapshot lines are stripped from the output; only the most recent is kept
        # (it is re-appended at EOF).
        if line_bytes.startswith(_ATTR_SNAP_PREFIX):
            last_snap = bytes(line_bytes)
            pos = line_end
            continue

        # Compact-boundary detection: byte-prefix pre-check, then a bounded marker search, then
        # JSON re-validation (the marker can appear inside user content).
        if line_bytes.startswith(_SYSTEM_PREFIX):
            marker_idx = line_bytes.find(_COMPACT_BOUNDARY_MARKER)
            if 0 <= marker_idx < _BOUNDARY_SEARCH_BOUND:
                # parseBoundaryLine is fed the line WITHOUT the trailing newline.
                content_end = nl if has_newline else line_end
                line_str = data[pos:content_end].decode("utf-8", "replace")
                hit = _parse_boundary_line(line_str)
                if hit is not None and hit["hasPreservedSegment"]:
                    # Don't truncate; preserved msgs are already in the output.
                    has_preserved_segment = True
                    out.extend(line_bytes)
                    pos = line_end
                    continue
                if hit is not None:
                    # Real boundary without preserved segment — reset output here.
                    out.clear()
                    boundary_start_offset = pos
                    has_preserved_segment = False
                    last_snap = None
                    pos = line_end
                    continue

        out.extend(line_bytes)
        pos = line_end

    # Re-append the surviving attr-snap at EOF (restore reads only the last one, so position
    # doesn't matter). Insert a separating LF if the output doesn't already end with one.
    if last_snap is not None:
        if len(out) > 0 and out[-1] != 0x0A:
            out.append(0x0A)
        out.extend(last_snap)

    return {
        "boundaryStartOffset": boundary_start_offset,
        "postBoundaryBuf": bytes(out),
        "hasPreservedSegment": has_preserved_segment,
    }
