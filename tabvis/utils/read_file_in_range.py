"""``read_file_in_range`` — line-oriented file reader.

Returns lines ``[offset, offset + max_lines)`` from a file, stripping a leading UTF-8 BOM and
normalizing ``\\r\\n``/``\\r``-suffixed lines to ``\\n``. The TS source splits into two code paths:

* **Fast path** (regular files < 10 MB): ``readFile`` then in-memory split.
* **Streaming path** (large files, FIFOs, devices): ``createReadStream`` with manual newline
  scanning so reading line 1 of a 100 GB file doesn't balloon memory.

Both paths are *behaviorally identical* for the same bytes — they differ only in memory profile
and how the ``maxBytes`` guard is enforced (pre-read on the fast path via the stat'd file size;
incrementally on the streaming path via accumulated bytes read). This Python implementation preserves that
distinction: regular files under :data:`FAST_PATH_MAX_SIZE` use the fast path (whole-file read +
split); everything else streams in chunks via :func:`_read_in_range_streaming`.

``max_bytes`` behavior depends on ``truncate_on_byte_limit``:

* ``False`` (default): legacy semantics — raise :class:`FileTooLargeError` if the FILE size
  (fast path) or total streamed bytes (streaming) exceed ``max_bytes``.
* ``True``: cap SELECTED OUTPUT at ``max_bytes``. Stop at the last complete line that fits and
  set ``truncatedByBytes`` in the result. Never raises.

Byte counts use UTF-8 encoding to match Node's ``Buffer.byteLength``.
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from tabvis.utils.abort import AbortSignal

# 10 MB — regular files smaller than this use the in-memory fast path.
FAST_PATH_MAX_SIZE = 10 * 1024 * 1024

# 512 KB highWaterMark, matching the TS createReadStream config.
_STREAM_CHUNK_SIZE = 512 * 1024

_BOM = "﻿"


# implemented). Inlined here so FileTooLargeError messages match the oracle byte-for-byte. Replace
# with `from tabvis.utils.format import format_file_size` once that module lands.
def _format_file_size(size_in_bytes: int) -> str:
    """E.g. ``1536 -> "1.5KB"``."""
    kb = size_in_bytes / 1024
    if kb < 1:
        return f"{size_in_bytes} bytes"
    if kb < 1024:
        return f"{_trim_zero(kb)}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{_trim_zero(mb)}MB"
    gb = mb / 1024
    return f"{_trim_zero(gb)}GB"


def _trim_zero(value: float) -> str:
    """``value.toFixed(1).replace(/\\.0$/, '')`` — one decimal, dropping a trailing ``.0``."""
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _byte_length(text: str) -> int:
    """Return the UTF-8 byte length of ``text``."""
    return len(text.encode("utf-8"))


class ReadFileRangeResult(TypedDict, total=False):
    """Wire shape returned to FileReadTool. Keys kept camelCase where the TS object used them.

    ``truncatedByBytes`` is present only when output was clipped under truncate mode.
    """

    content: str
    lineCount: int
    totalLines: int
    totalBytes: int
    readBytes: int
    mtimeMs: float
    truncatedByBytes: bool


class FileTooLargeError(Exception):
    """Raised in non-truncate mode when bytes exceed the cap."""

    def __init__(self, size_in_bytes: int, max_size_bytes: int) -> None:
        self.size_in_bytes = size_in_bytes
        self.max_size_bytes = max_size_bytes
        super().__init__(
            f"File content ({_format_file_size(size_in_bytes)}) exceeds maximum allowed size "
            f"({_format_file_size(max_size_bytes)}). Use offset and limit parameters to read "
            f"specific portions of the file, or search for specific content instead of reading "
            f"the whole file."
        )
        self.name = "FileTooLargeError"


async def read_file_in_range(
    file_path: str | os.PathLike[str],
    offset: int = 0,
    max_lines: int | None = None,
    max_bytes: int | None = None,
    signal: AbortSignal | None = None,
    truncate_on_byte_limit: bool = False,
) -> ReadFileRangeResult:
    """Read lines ``[offset, offset + max_lines)`` from ``file_path``.

    Mirrors the TS ``readFileInRange(filePath, offset, maxLines, maxBytes, signal, options)``
    public entry point. ``truncate_on_byte_limit`` corresponds to the TS
    ``options.truncateOnByteLimit``.
    """
    if signal is not None:
        signal.throw_if_aborted()

    path = Path(file_path)
    st = path.stat()  # follows symlinks, like fs.stat

    if stat_module.S_ISDIR(st.st_mode):
        raise OSError(
            f"EISDIR: illegal operation on a directory, read '{os.fspath(file_path)}'"
        )

    is_regular_file = stat_module.S_ISREG(st.st_mode)
    if is_regular_file and st.st_size < FAST_PATH_MAX_SIZE:
        if (
            not truncate_on_byte_limit
            and max_bytes is not None
            and st.st_size > max_bytes
        ):
            raise FileTooLargeError(st.st_size, max_bytes)

        raw = path.read_bytes().decode("utf-8", errors="replace")
        if signal is not None:
            signal.throw_if_aborted()
        return _read_in_range_fast(
            raw,
            st.st_mtime * 1000.0,
            offset,
            max_lines,
            max_bytes if truncate_on_byte_limit else None,
        )

    return _read_in_range_streaming(
        path,
        offset,
        max_lines,
        max_bytes,
        truncate_on_byte_limit,
        signal,
    )


def _strip_cr(line: str) -> str:
    """Drop a single trailing ``\\r`` (CRLF -> LF normalization)."""
    return line[:-1] if line.endswith("\r") else line


def _read_in_range_fast(
    raw: str,
    mtime_ms: float,
    offset: int,
    max_lines: int | None,
    truncate_at_bytes: int | None,
) -> ReadFileRangeResult:
    """Fast path — whole text in memory, split + select range."""
    end_line = offset + max_lines if max_lines is not None else None

    # Strip BOM.
    text = raw[1:] if raw[:1] == _BOM else raw

    selected_lines: list[str] = []
    selected_bytes = 0
    truncated_by_bytes = False

    def in_range(idx: int) -> bool:
        return idx >= offset and (end_line is None or idx < end_line)

    def try_push(line: str) -> bool:
        nonlocal selected_bytes, truncated_by_bytes
        if truncate_at_bytes is not None:
            sep = 1 if selected_lines else 0
            next_bytes = selected_bytes + sep + _byte_length(line)
            if next_bytes > truncate_at_bytes:
                truncated_by_bytes = True
                return False
            selected_bytes = next_bytes
        selected_lines.append(line)
        return True

    line_index = 0
    start_pos = 0
    while True:
        newline_pos = text.find("\n", start_pos)
        if newline_pos == -1:
            break
        if in_range(line_index) and not truncated_by_bytes:
            try_push(_strip_cr(text[start_pos:newline_pos]))
        line_index += 1
        start_pos = newline_pos + 1

    # Final fragment (no trailing newline).
    if in_range(line_index) and not truncated_by_bytes:
        try_push(_strip_cr(text[start_pos:]))
    line_index += 1

    content = "\n".join(selected_lines)
    result: ReadFileRangeResult = {
        "content": content,
        "lineCount": len(selected_lines),
        "totalLines": line_index,
        "totalBytes": _byte_length(text),
        "readBytes": _byte_length(content),
        "mtimeMs": mtime_ms,
    }
    if truncated_by_bytes:
        result["truncatedByBytes"] = True
    return result


def _read_in_range_streaming(
    path: Path,
    offset: int,
    max_lines: int | None,
    max_bytes: int | None,
    truncate_on_byte_limit: bool,
    signal: AbortSignal | None,
) -> ReadFileRangeResult:
    """Streaming path — chunked read with newline scanning.

    Lines outside the selected range are counted (for ``totalLines``) but discarded, so a
    huge file read with a tight range never accumulates the full content in memory.
    """
    end_line = offset + max_lines if max_lines is not None else None

    selected_lines: list[str] = []
    selected_bytes = 0
    truncated_by_bytes = False
    total_bytes_read = 0
    current_line_index = 0
    partial = ""
    is_first_chunk = True

    def in_range(idx: int) -> bool:
        return idx >= offset and (end_line is None or idx < end_line)

    mtime_ms = path.stat().st_mtime * 1000.0

    with path.open("rb") as fh:
        while True:
            if signal is not None:
                signal.throw_if_aborted()
            chunk_bytes = fh.read(_STREAM_CHUNK_SIZE)
            if not chunk_bytes:
                break
            chunk = chunk_bytes.decode("utf-8", errors="replace")

            if is_first_chunk:
                is_first_chunk = False
                if chunk[:1] == _BOM:
                    chunk = chunk[1:]

            total_bytes_read += _byte_length(chunk)
            if (
                not truncate_on_byte_limit
                and max_bytes is not None
                and total_bytes_read > max_bytes
            ):
                raise FileTooLargeError(total_bytes_read, max_bytes)

            data = partial + chunk if partial else chunk
            partial = ""

            start_pos = 0
            while True:
                newline_pos = data.find("\n", start_pos)
                if newline_pos == -1:
                    break
                if in_range(current_line_index):
                    line = _strip_cr(data[start_pos:newline_pos])
                    if truncate_on_byte_limit and max_bytes is not None:
                        sep = 1 if selected_lines else 0
                        next_bytes = selected_bytes + sep + _byte_length(line)
                        if next_bytes > max_bytes:
                            # Cap hit — collapse the selection range so nothing more is
                            # accumulated. Stream continues (to count totalLines).
                            truncated_by_bytes = True
                            end_line = current_line_index
                        else:
                            selected_bytes = next_bytes
                            selected_lines.append(line)
                    else:
                        selected_lines.append(line)
                current_line_index += 1
                start_pos = newline_pos + 1

            # Only keep the trailing fragment when inside the selected range.
            if start_pos < len(data) and in_range(current_line_index):
                fragment = data[start_pos:]
                if truncate_on_byte_limit and max_bytes is not None:
                    sep = 1 if selected_lines else 0
                    frag_bytes = selected_bytes + sep + _byte_length(fragment)
                    if frag_bytes > max_bytes:
                        truncated_by_bytes = True
                        end_line = current_line_index
                        partial = ""
                        continue
                partial = fragment

    # End-of-stream: flush the final partial line.
    line = _strip_cr(partial)
    if in_range(current_line_index):
        if truncate_on_byte_limit and max_bytes is not None:
            sep = 1 if selected_lines else 0
            next_bytes = selected_bytes + sep + _byte_length(line)
            if next_bytes > max_bytes:
                truncated_by_bytes = True
            else:
                selected_lines.append(line)
        else:
            selected_lines.append(line)
    current_line_index += 1

    content = "\n".join(selected_lines)
    result: ReadFileRangeResult = {
        "content": content,
        "lineCount": len(selected_lines),
        "totalLines": current_line_index,
        "totalBytes": total_bytes_read,
        "readBytes": _byte_length(content),
        "mtimeMs": mtime_ms,
    }
    if truncated_by_bytes:
        result["truncatedByBytes"] = True
    return result
