"""Sync file-read path

Extracted from ``file.ts`` in the TS tree to break a strongly-connected-component import
cycle: ``file.ts`` reaches the settings SCC via ``log.ts`` → ``types/logs.ts`` → … ;
anything needing ``readFileSync`` from ``file.ts`` pulled in the whole chain. This leaf
imports only :mod:`tabvis.utils.fs_operations` and :mod:`tabvis.utils.debug`, both of which
terminate in stdlib calls.

``detectFileEncoding`` / ``detectLineEndings`` stay in ``file.ts`` (they call ``logError`` on
unexpected failures, which re-enters the SCC). The ``*ForResolvedPath`` / ``*ForString``
helpers here are the pure parts; callers needing the logging wrappers import from ``file.ts``.

Casing: snake_case identifiers; :data:`LineEndingType` mirrors the TS string-literal union
(``'CRLF' | 'LF'``). Encodings are the Node ``BufferEncoding`` literals (``'utf8'`` / ``'utf16le'``)
kept verbatim so they match what :mod:`tabvis.utils.file` produces and consumes.
"""

from __future__ import annotations

from typing import Literal

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.fs_operations import get_fs_implementation, safe_resolve_path

LineEndingType = Literal["CRLF", "LF"]

# Node BufferEncoding subset this module emits (matches detectFileEncoding's domain).
BufferEncoding = Literal["utf8", "utf16le"]


def detect_encoding_for_resolved_path(resolved_path: str) -> BufferEncoding:
    """Detect the text encoding of an already-resolved path by sniffing the BOM / first bytes.

    Reads up to the first 4096 bytes and inspects the byte-order mark:

    - empty file → ``'utf8'`` (NOT ascii; writing emojis/CJK to an empty file otherwise
      corrupts);
    - ``0xFF 0xFE`` → ``'utf16le'``;
    - ``0xEF 0xBB 0xBF`` → ``'utf8'`` (UTF-8 BOM);
    - otherwise ``'utf8'`` (a superset of ascii that handles all Unicode).
    """
    result = get_fs_implementation().read_sync(resolved_path, {"length": 4096})
    buffer = result["buffer"]
    bytes_read = result["bytes_read"]

    # Empty files default to utf8, not ascii.
    if bytes_read == 0:
        return "utf8"

    if bytes_read >= 2 and buffer[0] == 0xFF and buffer[1] == 0xFE:
        return "utf16le"

    if (
        bytes_read >= 3
        and buffer[0] == 0xEF
        and buffer[1] == 0xBB
        and buffer[2] == 0xBF
    ):
        return "utf8"

    # For non-empty files, default to utf8 (superset of ascii, full Unicode support).
    return "utf8"


def detect_line_endings_for_string(content: str) -> LineEndingType:
    """Classify a string's dominant line-ending style as ``'CRLF'`` or ``'LF'``.

    Counts ``\\n`` preceded by ``\\r`` (CRLF) vs not (LF); ``'CRLF'`` only when it strictly
    outnumbers ``'LF'`` (ties → ``'LF'``).
    """
    crlf_count = 0
    lf_count = 0

    for i in range(len(content)):
        if content[i] == "\n":
            if i > 0 and content[i - 1] == "\r":
                crlf_count += 1
            else:
                lf_count += 1

    return "CRLF" if crlf_count > lf_count else "LF"


def read_file_sync_with_metadata(file_path: str) -> dict[str, object]:
    """Like ``read_file_sync`` but also returns the detected encoding and original line-ending
    style in one filesystem pass.

    Returns a plain dict ``{"content": str, "encoding": BufferEncoding, "lineEndings":
    LineEndingType}`` (camelCase ``lineEndings`` key kept from the TS return shape). Callers
    writing the file back (e.g. FileEditTool) can reuse these instead of re-running
    ``detectFileEncoding`` / ``detectLineEndings`` (each of which redoes safeResolvePath +
    readSync(4KB)).
    """
    fs = get_fs_implementation()
    resolved = safe_resolve_path(fs, file_path)
    resolved_path = resolved["resolved_path"]
    is_symlink = resolved["is_symlink"]

    if is_symlink:
        log_for_debugging(f"Reading through symlink: {file_path} -> {resolved_path}")

    encoding = detect_encoding_for_resolved_path(resolved_path)
    raw = fs.read_file_sync(resolved_path, {"encoding": encoding})
    # Detect line endings from the raw head BEFORE CRLF normalization erases the distinction.
    # The TS reads ``raw.slice(0, 4096)`` from a non-newline-translated ``readFileSync``; the
    # implemented ``read_file_sync`` opens in text mode (Python's universal-newline translation strips
    # ``\r\n`` → ``\n``), so we sniff the head via ``read_sync`` (raw bytes, NO translation) —
    # this is exactly how the TS ``detectLineEndings`` reads (``fs.readSync(path, {length:4096})``).
    head_result = fs.read_sync(resolved_path, {"length": 4096})
    head = head_result["buffer"][: head_result["bytes_read"]].decode(
        "utf-16-le" if encoding == "utf16le" else "utf-8", errors="replace"
    )
    line_endings = detect_line_endings_for_string(head)
    return {
        "content": raw.replace("\r\n", "\n"),
        "encoding": encoding,
        "lineEndings": line_endings,
    }


def read_file_sync(file_path: str) -> str:
    """Read a file to a string, normalizing CRLF → LF (encoding auto-detected)."""
    return read_file_sync_with_metadata(file_path)["content"]  # type: ignore[return-value]
