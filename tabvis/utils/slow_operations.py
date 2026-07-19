r"""Slow-operation logging + wrapped JSON/clone/fs helpers

Wraps the hot built-ins (``JSON.stringify`` / ``JSON.parse`` / ``structuredClone`` /
lodash ``cloneDeep`` / ``fs.writeFileSync``) so an ANT build can time them and surface any
that exceed :data:`SLOW_OPERATION_THRESHOLD_MS` to the dev bar (via
:func:`tabvis.bootstrap.state.add_slow_operation`) and the debug log.

Faithful-behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- The TS ``slowLogging`` tagged template is gated behind ``false ? slowLoggingAnt :
  slowLoggingExternal`` — i.e. it is a **no-op disposable** in this (external) build, with the
  ANT path dead-code-eliminated. We keep that gate exactly: :data:`_ANT_SLOW_LOGGING` is
  ``False``, so :func:`slow_logging` returns the no-op context manager and the timing/stack
  machinery is never entered. (Set :data:`_ANT_SLOW_LOGGING` to ``True`` to exercise the ANT
  path — the timing/threshold/log behaviour is fully implemented under it.)
- Python has no tagged-template literals, so the ``slowLogging`\`...\``` call sites become
  ``with slow_logging(description):`` where ``description`` is the already-built string
  (matching what ``buildDescription`` would produce). The lazy ANT-only ``buildDescription``
  is preserved as :func:`build_description` for callers that pass template-style parts.
- ``structuredClone`` and lodash ``cloneDeep`` both map to :func:`copy.deepcopy` (the closest
  stdlib equivalent; both produce a deep structural copy).
- ``performance.now()`` → :func:`time.perf_counter` (×1000 for ms).
"""

from __future__ import annotations

import copy
import json
import math
import os
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from tabvis.bootstrap.state import add_slow_operation
from tabvis.utils.debug import log_for_debugging

_T = TypeVar("_T")

# --- Slow operation logging infrastructure ---


def _compute_threshold_ms() -> float:
    """Threshold in milliseconds for logging slow JSON/clone operations.

    Operations taking longer than this will be logged for debugging.
    - Override: set ``TABVIS_SLOW_OPERATION_THRESHOLD_MS`` to a number
    - Dev builds: 20ms (lower threshold for development)
    - Ants: 300ms (enabled for all internal users)
    """
    env_value = os.environ.get("TABVIS_SLOW_OPERATION_THRESHOLD_MS")
    if env_value is not None:
        try:
            parsed = float(env_value)
        except ValueError:
            parsed = math.nan
        if not math.isnan(parsed) and parsed >= 0:
            return parsed
    if os.environ.get("NODE_ENV") == "development":
        return 20
    return math.inf


SLOW_OPERATION_THRESHOLD_MS: float = _compute_threshold_ms()

# Module-level re-entrancy guard. log_for_debugging writes to a debug file via append, which
# goes back through slow_logging. Without this guard, a slow append → dispose →
# log_for_debugging → append → dispose → ... would recurse.
_is_logging = False


def caller_frame(stack: str | None) -> str:
    """Extract the first stack frame outside this file, so the DevBar warning points at the
    actual caller instead of a useless ``Object{N keys}``.

    Only called when an operation was actually slow — never on the fast path.
    """
    if not stack:
        return ""
    for line in stack.split("\n"):
        if "slow_operations" in line:
            continue
        # Match a trailing `<file>:<line>:<col>` (the col is dropped). Mirrors the TS regex
        # ``/([^/\\]+?):(\d+):\d+\)?$/`` adapted to CPython traceback frame lines, which look
        # like `  File ".../foo.py", line 12, in bar`.
        for candidate in (
            _match_traceback_frame(line),
            _match_v8_frame(line),
        ):
            if candidate:
                return f" @ {candidate}"
    return ""


def _match_traceback_frame(line: str) -> str | None:
    # CPython: `  File "/abs/path/foo.py", line 12, in bar`
    stripped = line.strip()
    if not stripped.startswith("File "):
        return None
    try:
        path_part, rest = stripped[len('File "') :].split('"', 1)
    except ValueError:
        return None
    base = path_part.replace("\\", "/").rsplit("/", 1)[-1]
    # rest looks like `, line 12, in bar`
    parts = rest.split("line ", 1)
    if len(parts) < 2:
        return None
    line_no = parts[1].split(",", 1)[0].strip()
    if not line_no.isdigit():
        return None
    return f"{base}:{line_no}"


def _match_v8_frame(line: str) -> str | None:
    # V8/JSC-style: `... (/abs/path/foo.js:12:34)` — kept for parity with the TS regex.
    import re

    m = re.search(r"([^/\\]+?):(\d+):\d+\)?$", line)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    return None


def build_description(parts: list[Any]) -> str:
    """Build a human-readable description from template-style parts.

    Only called when an operation was actually slow — never on the fast path. ``parts`` is the
    interleaved sequence of literal strings and interpolated values that the TS tagged template
    would have received (``args[0]`` = the strings array, ``args[1..n]`` = the values). Here we
    accept the values directly and format each one the way ``buildDescription`` does.
    """
    result = ""
    for v in parts:
        if isinstance(v, str):
            result += v
        elif isinstance(v, (list, tuple)):
            result += f"Array[{len(v)}]"
        elif v is not None and isinstance(v, dict):
            result += f"Object{{{len(v)} keys}}"
        else:
            result += str(v)
    return result


class _AntSlowLogger:
    """ANT-only timing context manager. Times the wrapped operation and, if it exceeds the
    threshold, logs it for debugging and records it on the dev bar. Mirrors the TS
    ``AntSlowLogger`` (constructor captures start time + stack; ``[Symbol.dispose]`` reports)."""

    __slots__ = ("start_time", "description", "_stack")

    def __init__(self, description: str) -> None:
        self.start_time = time.perf_counter() * 1000
        self.description = description
        # Capture the stack at construction (cheap); format lazily only when slow.
        self._stack = traceback.extract_stack()

    def dispose(self) -> None:
        global _is_logging
        duration = time.perf_counter() * 1000 - self.start_time
        if duration > SLOW_OPERATION_THRESHOLD_MS and not _is_logging:
            _is_logging = True
            try:
                stack_str = "".join(traceback.format_list(self._stack))
                description = self.description + caller_frame(stack_str)
                log_for_debugging(
                    f"[SLOW OPERATION DETECTED] {description} ({duration:.1f}ms)"
                )
                add_slow_operation(description, int(duration))
            finally:
                _is_logging = False


@contextmanager
def _ant_slow_logging(description: str) -> Iterator[None]:
    logger = _AntSlowLogger(description)
    try:
        yield
    finally:
        logger.dispose()


@contextmanager
def _noop_slow_logging(description: str) -> Iterator[None]:  # noqa: ARG001 - parity with ANT signature
    yield


# Tagged-template equivalent for slow operation logging.
#
# In ANT builds: returns a context manager that times the operation and logs if it exceeds the
# threshold. Description is built by the caller (lazily would-be) only when slow.
#
# In external builds: returns a no-op context manager. Zero timing, zero allocations. The ANT
# logger and build_description are effectively dead code (only reachable when the flag flips).
#
# Matches the TS gate ``export const slowLogging = false ? slowLoggingAnt : slowLoggingExternal``.
_ANT_SLOW_LOGGING = False

slow_logging: Callable[[str], Any] = (
    _ant_slow_logging if _ANT_SLOW_LOGGING else _noop_slow_logging
)


# --- Wrapped operations ---


def json_stringify(
    value: Any,
    replacer: Any = None,
    space: str | int | None = None,
) -> str:
    """Wrapped JSON serialization with slow-operation logging. Use instead of a raw
    ``json.dumps`` so performance issues surface on the dev bar.

    ``replacer`` accepts either a list of allowed keys (like the JS array replacer) or ``None``.
    A function replacer (the JS ``(key, value) => …`` form) is not supported here — callers in
    the existing tree pass either nothing or an allow-list / indent. ``space`` maps to ``indent``.
    """
    with slow_logging("JSON.stringify(<value>)"):
        return _json_stringify_impl(value, replacer, space)


def _json_stringify_impl(value: Any, replacer: Any, space: str | int | None) -> str:
    indent: int | str | None
    if isinstance(space, (int, str)):
        indent = space
    else:
        indent = None
    # JS JSON.stringify uses `,` / `: ` separators when no indent; Python defaults differ
    # (`, ` / `: `). Match JS compact output (no spaces) when there is no indent.
    if indent is None:
        separators: tuple[str, str] | None = (",", ":")
    else:
        separators = None

    obj = value
    if isinstance(replacer, (list, tuple)):
        allowed = {str(k) for k in replacer}
        obj = _filter_keys(value, allowed)

    return json.dumps(obj, indent=indent, separators=separators, ensure_ascii=False)


def _filter_keys(value: Any, allowed: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _filter_keys(v, allowed) for k, v in value.items() if k in allowed}
    if isinstance(value, list):
        return [_filter_keys(v, allowed) for v in value]
    return value


def json_parse(text: str, reviver: Callable[[str, Any], Any] | None = None) -> Any:
    """Wrapped JSON parsing with slow-operation logging. Use instead of a raw ``json.loads`` so
    performance issues surface on the dev bar.

    ``reviver`` mirrors the JS ``(key, value) => …`` second argument: when provided it is
    applied bottom-up to every key/value pair (and finally the root under the empty key).
    """
    with slow_logging("JSON.parse(<text>)"):
        if reviver is None:
            return json.loads(text)
        return _apply_reviver(json.loads(text), reviver)


def _apply_reviver(value: Any, reviver: Callable[[str, Any], Any]) -> Any:
    def walk(holder: Any, key: str) -> Any:
        val = holder[key] if isinstance(holder, dict) else holder
        if isinstance(val, dict):
            for k in list(val.keys()):
                new_v = walk(val, k)
                if new_v is None:
                    del val[k]
                else:
                    val[k] = new_v
        elif isinstance(val, list):
            for i in range(len(val)):
                new_v = walk({i: val[i]}, i)  # type: ignore[index]
                val[i] = new_v
        return reviver(str(key), val)

    return walk({"": value}, "")


def clone(value: _T) -> _T:
    """Wrapped deep clone (``structuredClone`` equivalent) with slow-operation logging."""
    with slow_logging("structuredClone(<value>)"):
        return copy.deepcopy(value)


def clone_deep(value: _T) -> _T:
    """Wrapped deep clone (lodash ``cloneDeep`` equivalent) with slow-operation logging."""
    with slow_logging("cloneDeep(<value>)"):
        return copy.deepcopy(value)


def write_file_sync_deprecated(
    file_path: str,
    data: str | bytes,
    options: dict[str, Any] | None = None,
) -> None:
    """Wrapper around a synchronous file write with slow-operation logging.

    Supports a ``flush`` option to ensure data is written to disk before returning (open →
    write → fsync → close), mirroring ``writeFileSync_DEPRECATED``.

    .. deprecated:: Prefer async writes. Sync file writes block and cause performance issues.
    """
    with slow_logging(f"fs.writeFileSync({file_path}, <data>)"):
        needs_flush = (
            options is not None
            and isinstance(options, dict)
            and options.get("flush") is True
        )

        if needs_flush:
            encoding = options.get("encoding") if options else None
            mode = options.get("mode") if options else None
            payload: bytes = (
                data
                if isinstance(data, bytes)
                else data.encode(encoding or "utf-8")
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(file_path, flags, mode) if mode is not None else os.open(file_path, flags)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            # No flush needed, use a standard write.
            mode_str = "wb" if isinstance(data, bytes) else "w"
            encoding = None if isinstance(data, bytes) else (
                (options or {}).get("encoding") or "utf-8"
            )
            with open(file_path, mode_str, encoding=encoding) as f:
                f.write(data)
