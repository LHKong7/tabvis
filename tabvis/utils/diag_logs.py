"""Diagnostic (no-PII) logging

Appends structured diagnostic entries to the logfile named by ``TABVIS_DIAGNOSTICS_FILE``. The
environment manager forwards these to session-ingress to monitor issues from inside the
container.

*Important* — :func:`log_for_diagnostics_no_pii` MUST NOT be called with any PII (file paths,
project/repo names, prompts, etc.).

The log entry is a JSONL line written through :func:`tabvis.utils.slow_operations.json_stringify`,
via the swappable :func:`tabvis.utils.fs_operations.get_fs_implementation`. Per
``docs/SPINE_CONTRACTS.md`` the entry is a plain wire dict — keys (``timestamp`` / ``level`` /
``event`` / ``data``, and ``duration_ms`` inside the timing payload) are kept verbatim.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from os.path import dirname
from typing import Any, Literal, TypeVar

from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.slow_operations import json_stringify

DiagnosticLogLevel = Literal["debug", "info", "warn", "error"]

_T = TypeVar("_T")


def _iso_now() -> str:
    """``new Date().toISOString()`` — UTC ISO-8601 with millisecond precision and a ``Z`` suffix."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def log_for_diagnostics_no_pii(
    level: DiagnosticLogLevel,
    event: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Log diagnostic information to the ``TABVIS_DIAGNOSTICS_FILE`` logfile.

    *Important* — MUST NOT be called with any PII. ``level`` is informational only (not used for
    filtering); ``event`` is a specific event name ("started", "mcp_connected", …).
    """
    log_file = _get_diagnostic_log_file()
    if not log_file:
        return

    entry: dict[str, Any] = {
        "timestamp": _iso_now(),
        "level": level,
        "event": event,
        "data": data if data is not None else {},
    }

    fs = get_fs_implementation()
    line = json_stringify(entry) + "\n"
    try:
        fs.append_file_sync(log_file, line)
    except Exception:  # noqa: BLE001
        # If append fails, try creating the directory first.
        try:
            fs.mkdir_sync(dirname(log_file))
            fs.append_file_sync(log_file, line)
        except Exception:  # noqa: BLE001
            # Silently fail if logging is not possible.
            pass


def _get_diagnostic_log_file() -> str | None:
    return os.environ.get("TABVIS_DIAGNOSTICS_FILE")


async def with_diagnostics_timing(
    event: str,
    fn: Callable[[], Awaitable[_T]],
    get_data: Callable[[_T], dict[str, Any]] | None = None,
) -> _T:
    """Wrap an async function with diagnostic timing logs.

    Logs ``{event}_started`` before execution and ``{event}_completed`` after with
    ``duration_ms`` (plus any extra data from ``get_data``); on error logs ``{event}_failed``
    with ``duration_ms`` and re-raises.
    """
    start_time = _now_ms()
    log_for_diagnostics_no_pii("info", f"{event}_started")

    try:
        result = await fn()
        additional_data = get_data(result) if get_data else {}
        log_for_diagnostics_no_pii(
            "info",
            f"{event}_completed",
            {"duration_ms": _now_ms() - start_time, **additional_data},
        )
        return result
    except BaseException:
        log_for_diagnostics_no_pii(
            "error",
            f"{event}_failed",
            {"duration_ms": _now_ms() - start_time},
        )
        raise


def _now_ms() -> int:
    """``Date.now()`` — milliseconds since the Unix epoch as an int."""
    from time import time

    return int(time() * 1000)
