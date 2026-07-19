"""Tolerant JSON / JSONC / JSONL helpers

Surface:

* :func:`safe_parse_json` — memoized (LRU-bounded, small inputs only), BOM-stripping,
  *never-throws* ``JSON.parse``. Returns ``None`` on invalid input (logging the error once
  per distinct string unless suppressed).
* :func:`safe_parse_jsonc` — tolerant parse of JSON-with-comments (keybindings.json etc.).
* :func:`parse_jsonl` — parse JSONL (one JSON value per line), skipping malformed lines.
* :func:`read_jsonl_file` — read + parse a JSONL file, reading at most the last 100 MB.
* :func:`add_item_to_jsonc_array` — append an item to a JSONC array, preserving comments
  where possible (falls back to a clean re-serialization).

Behavior and dependency notes (per ``docs/SPINE_CONTRACTS.md``):
- ``stripBOM`` → :func:`tabvis.utils.json_read.strip_bom`. ``logError`` →
  :func:`tabvis.utils.log.log_error`. ``jsonStringify`` →
  :func:`tabvis.utils.slow_operations.json_stringify`.
- lodash/lru ``memoizeWithLRU(parseJSONUncached, json => json, 50)`` →
  :func:`tabvis.utils.memoize.memoize_with_lru` (the richer LRU memoizer the TS uses — NOT a bare
  ``lru_cache``). The discriminated-union ``CachedParse`` wrapper is preserved so invalid JSON is
  also cached (no re-parse/re-log on repeated bad input) and ``null`` (a valid parse) is cacheable.
  ``shouldLogError`` is intentionally excluded from the cache key (first-arg-only resolver).
- ``Bun.JSONL.parseChunk`` (a Bun-only fast path) has no Python analogue → dropped; the
  ``indexOf``-based string/bytes scanners are the faithful fallback (which the TS itself uses on
  non-Bun runtimes), so behavior is identical here.
- ``jsonc-parser`` (``parse``/``modify``/``applyEdits``) is an npm dependency with no stdlib
  equivalent. The JSONC *tolerance* (``//`` + ``/* */`` comments, trailing commas) is reimplemented
  with a small stdlib tokenizer + :func:`json.loads`. ``addItemToJSONCArray`` keeps the
  parse→append→serialize behavior but cannot preserve interleaved comments byte-for-byte the way
  ``modify``/``applyEdits`` do; it falls back to a clean :func:`json_stringify` re-serialization
  (the TS fallback branch when ``modify`` returns no edits). Recorded as a stdlib substitution.

Casing: Python identifiers are snake_case. The parsed values are arbitrary JSON (dicts keep their
verbatim wire keys); this module never renames keys.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, TypeVar

from tabvis.utils.json_read import strip_bom
from tabvis.utils.log import log_error
from tabvis.utils.memoize import memoize_with_lru
from tabvis.utils.slow_operations import json_stringify

_T = TypeVar("_T")

# Skip caching above this size — the LRU stores the full string as the key, so a 200KB config
# file would pin ~10MB across 50 slots. Large inputs (e.g. ~/.tabvis.json) also change between
# reads, so the cache never hits anyway.
PARSE_CACHE_MAX_KEY_BYTES = 8 * 1024


def _parse_json_uncached(json_str: str, should_log_error: bool) -> dict[str, Any]:
    """Inner parse returning a discriminated-union wrapper ``{ok: bool, value?: ...}``.

    A plain dict envelope (not pydantic) — purely an internal cache value, never serialized.
    """
    try:
        return {"ok": True, "value": json.loads(strip_bom(json_str))}
    except (ValueError, TypeError) as e:
        if should_log_error:
            log_error(e)
        return {"ok": False}


# Memoized inner parse (LRU-bounded to 50 entries; keys on the first arg only). Invalid JSON is
# cached too — otherwise repeated calls with the same bad string re-parse and re-log every time.
_parse_json_cached = memoize_with_lru(_parse_json_uncached, lambda json_str, *_: json_str, 50)


def safe_parse_json(json_str: str | None, should_log_error: bool = True) -> Any:
    """Safely parse JSON, returning ``None`` on any failure (never raises).

    Memoized for performance (LRU-bounded to 50 entries, small inputs only). Falsy input
    (``None``/empty) → ``None``.
    """
    if not json_str:
        return None
    result = (
        _parse_json_uncached(json_str, should_log_error)
        if len(json_str) > PARSE_CACHE_MAX_KEY_BYTES
        else _parse_json_cached(json_str, should_log_error)
    )
    return result["value"] if result["ok"] else None


# Expose the underlying LRU cache handle (parity with the TS ``Object.assign(..., { cache })``).
safe_parse_json.cache = _parse_json_cached.cache  # type: ignore[attr-defined]


def safe_parse_jsonc(json_str: str | None) -> Any:
    """Safely parse JSON-with-comments (jsonc), returning ``None`` on failure.

    Useful for VS Code configuration files (keybindings.json etc.) which support comments and
    trailing commas. Strips a BOM before parsing (PowerShell 5.x adds one to UTF-8 files).
    """
    if not json_str:
        return None
    try:
        return json.loads(_strip_jsonc(strip_bom(json_str)))
    except (ValueError, TypeError) as e:
        log_error(e)
        return None


def _strip_jsonc(text: str) -> str:
    """Strip ``//`` line comments, ``/* */`` block comments, and trailing commas from ``text``.

    A small hand-rolled tokenizer that skips string literals (so comment/comma markers inside a
    string are left intact). This is the stdlib stand-in for ``jsonc-parser``'s tolerant parse.
    """
    out: list[str] = []
    n = len(text)
    i = 0
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        # Not in a string.
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            # Line comment — skip to end of line.
            i += 2
            while i < n and text[i] not in ("\n", "\r"):
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            # Block comment — skip to the closing */.
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1

    stripped = "".join(out)
    return _strip_trailing_commas(stripped)


def _strip_trailing_commas(text: str) -> str:
    """Remove commas that immediately precede a closing ``}`` or ``]`` (ignoring whitespace).

    Skips string literals so a comma-before-brace inside a string is preserved.
    """
    out: list[str] = []
    n = len(text)
    i = 0
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            # Look ahead past whitespace for a closing bracket.
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                # Drop this trailing comma.
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_jsonl_string(data: str) -> list[Any]:
    stripped = strip_bom(data)
    results: list[Any] = []
    for raw in stripped.split("\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except (ValueError, TypeError):
            # Skip malformed lines.
            continue
    return results


def _parse_jsonl_buffer(buf: bytes) -> list[Any]:
    start = 0
    # Strip UTF-8 BOM (EF BB BF).
    if buf[:3] == b"\xef\xbb\xbf":
        start = 3
    results: list[Any] = []
    for raw in buf[start:].split(b"\n"):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except (ValueError, TypeError):
            # Skip malformed lines.
            continue
    return results


def parse_jsonl(data: str | bytes) -> list[Any]:
    """Parse JSONL data (one JSON value per line) from a string or bytes, skipping malformed lines.

    Uses an ``indexOf``-style line scan (the non-Bun fallback; Bun's ``JSONL.parseChunk`` fast
    path has no Python equivalent and is dropped — behavior is identical).
    """
    if isinstance(data, (bytes, bytearray)):
        return _parse_jsonl_buffer(bytes(data))
    return _parse_jsonl_string(data)


MAX_JSONL_READ_BYTES = 100 * 1024 * 1024


async def read_jsonl_file(file_path: str) -> list[Any]:
    """Read and parse a JSONL file, reading at most the last 100 MB.

    For files larger than 100 MB, reads the tail and skips the first (partial) line. 100 MB is
    ample: the largest context window we support is ~2M tokens, well under 100 MB of JSONL.
    """
    size = (await asyncio.to_thread(os.stat, file_path)).st_size
    if size <= MAX_JSONL_READ_BYTES:
        data = await asyncio.to_thread(_read_bytes, file_path)
        return parse_jsonl(data)

    def _read_tail() -> bytes:
        with open(file_path, "rb") as f:
            f.seek(size - MAX_JSONL_READ_BYTES)
            return f.read(MAX_JSONL_READ_BYTES)

    buf = await asyncio.to_thread(_read_tail)
    # Skip the first partial line.
    newline_index = buf.find(b"\n")
    if newline_index != -1 and newline_index < len(buf) - 1:
        return parse_jsonl(buf[newline_index + 1 :])
    return parse_jsonl(buf)


def _read_bytes(file_path: str) -> bytes:
    with open(file_path, "rb") as f:
        return f.read()


def add_item_to_jsonc_array(content: str, new_item: Any) -> str:
    """Append ``new_item`` to a JSONC array, returning the modified JSONC string.

    Mirrors the TS branches: empty/whitespace content → a fresh ``[new_item]`` (4-space indent);
    a valid array → the array with the item appended; anything else (non-array, parse failure) →
    a fresh ``[new_item]``.

    Note: the TS path uses ``jsonc-parser``'s ``modify``/``applyEdits`` to preserve interleaved
    comments byte-for-byte. There is no stdlib equivalent, so this implementation re-serializes the parsed
    array via :func:`json_stringify` — equivalent to the TS fallback branch (when ``modify``
    returns no edits). Comments are not preserved; values are.
    """
    try:
        # Empty / whitespace-only content → create a new JSON file.
        if not content or content.strip() == "":
            return json_stringify([new_item], None, 4)

        # Strip BOM before parsing (PowerShell 5.x adds BOM to UTF-8 files).
        clean_content = strip_bom(content)

        parsed_content = json.loads(_strip_jsonc(clean_content))

        if isinstance(parsed_content, list):
            return json_stringify([*parsed_content, new_item], None, 4)
        # Not an array at all → replace it completely with a new array.
        return json_stringify([new_item], None, 4)
    except Exception as e:  # noqa: BLE001 - parity with the TS catch-all
        log_error(e)
        return json_stringify([new_item], None, 4)
