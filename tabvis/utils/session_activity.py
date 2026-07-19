"""Session activity tracking with refcount-based heartbeat timer.

The transport registers its keep-alive sender via :func:`register_session_activity_callback`.
Callers (API streaming, tool execution) bracket their work with
:func:`start_session_activity` / :func:`stop_session_activity`. When the refcount is >0 a
periodic timer fires the registered callback every 30 seconds to keep the container alive.

Sending keep-alives is gated behind ``TABVIS_REMOTE_SEND_KEEPALIVES``. Diagnostic logging
always fires to help diagnose idle gaps.

Faithful-behavior notes:
- The TS module is process-global and loop-agnostic (``setInterval``/``setTimeout`` run on the
  Node event loop with no ``await``). The implementation mirrors that with module-level state and
  :class:`threading.Timer` (a recurring heartbeat re-arms itself; the idle timer is one-shot),
  matching the ``buffered_writer.py`` precedent so no running asyncio loop is required.
- ``activeReasons`` is a ``Map<SessionActivityReason, number>`` — a ref-count *per reason*. The
  implementation keeps a plain ``dict[str, int]``; the shutdown cleanup serializes it as ``active`` exactly
  like ``Object.fromEntries(activeReasons)``.
- ``Date.now()`` → milliseconds since epoch (int), matching ``oldest_activity_ms`` arithmetic.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Literal

from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.diag_logs import log_for_diagnostics_no_pii
from tabvis.utils.env_utils import is_env_truthy

SESSION_ACTIVITY_INTERVAL_MS = 30_000

SessionActivityReason = Literal["api_call", "tool_exec"]

_activity_callback = None  # type: ignore[var-annotated]  # Callable[[], None] | None
_refcount = 0
_active_reasons: dict[str, int] = {}
_oldest_activity_started_at: int | None = None
_heartbeat_timer: threading.Timer | None = None
_idle_timer: threading.Timer | None = None
_cleanup_registered = False


def _now_ms() -> int:
    """``Date.now()`` — milliseconds since the Unix epoch as an int."""
    return int(time.time() * 1000)


def _heartbeat_tick() -> None:
    log_for_diagnostics_no_pii("debug", "session_keepalive_heartbeat", {"refcount": _refcount})
    if is_env_truthy(os.environ.get("TABVIS_REMOTE_SEND_KEEPALIVES")):
        if _activity_callback is not None:
            _activity_callback()
    # Re-arm to emulate ``setInterval`` (only while still the active timer).
    _rearm_heartbeat()


def _rearm_heartbeat() -> None:
    global _heartbeat_timer
    if _heartbeat_timer is None:
        # Cancelled between firings — do not re-arm.
        return
    timer = threading.Timer(SESSION_ACTIVITY_INTERVAL_MS / 1000, _heartbeat_tick)
    timer.daemon = True
    _heartbeat_timer = timer
    timer.start()


def _start_heartbeat_timer() -> None:
    global _heartbeat_timer
    _clear_idle_timer()
    timer = threading.Timer(SESSION_ACTIVITY_INTERVAL_MS / 1000, _heartbeat_tick)
    timer.daemon = True
    _heartbeat_timer = timer
    timer.start()


def _start_idle_timer() -> None:
    global _idle_timer
    _clear_idle_timer()
    if _activity_callback is None:
        return

    def _fire() -> None:
        global _idle_timer
        log_for_diagnostics_no_pii("info", "session_idle_30s")
        _idle_timer = None

    timer = threading.Timer(SESSION_ACTIVITY_INTERVAL_MS / 1000, _fire)
    timer.daemon = True
    _idle_timer = timer
    timer.start()


def _clear_idle_timer() -> None:
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None


def register_session_activity_callback(cb) -> None:
    """Register the transport keep-alive sender."""
    global _activity_callback
    _activity_callback = cb
    # Restart timer if work is already in progress (e.g. reconnect during streaming).
    if _refcount > 0 and _heartbeat_timer is None:
        _start_heartbeat_timer()


def unregister_session_activity_callback() -> None:
    """Remove the keep-alive sender and stop the heartbeat/idle timers."""
    global _activity_callback, _heartbeat_timer
    _activity_callback = None
    # Stop timer if the callback is removed.
    if _heartbeat_timer is not None:
        _heartbeat_timer.cancel()
        _heartbeat_timer = None
    _clear_idle_timer()


def send_session_activity_signal() -> None:
    """Fire the keep-alive once, gated on ``TABVIS_REMOTE_SEND_KEEPALIVES``."""
    if is_env_truthy(os.environ.get("TABVIS_REMOTE_SEND_KEEPALIVES")):
        if _activity_callback is not None:
            _activity_callback()


def is_session_activity_tracking_active() -> bool:
    """Whether a keep-alive callback is currently registered."""
    return _activity_callback is not None


def start_session_activity(reason: SessionActivityReason) -> None:
    """Increment the activity refcount.

    When it transitions from 0→1 and a callback is registered, start a periodic heartbeat timer.
    """
    global _refcount, _oldest_activity_started_at, _cleanup_registered
    _refcount += 1
    _active_reasons[reason] = _active_reasons.get(reason, 0) + 1
    if _refcount == 1:
        _oldest_activity_started_at = _now_ms()
        if _activity_callback is not None and _heartbeat_timer is None:
            _start_heartbeat_timer()
    if not _cleanup_registered:
        _cleanup_registered = True

        async def _on_shutdown() -> None:
            log_for_diagnostics_no_pii(
                "info",
                "session_activity_at_shutdown",
                {
                    "refcount": _refcount,
                    "active": dict(_active_reasons),
                    # Only meaningful while work is in-flight; stale otherwise.
                    "oldest_activity_ms": (
                        _now_ms() - _oldest_activity_started_at
                        if _refcount > 0 and _oldest_activity_started_at is not None
                        else None
                    ),
                },
            )

        register_cleanup(_on_shutdown)


def stop_session_activity(reason: SessionActivityReason) -> None:
    """Decrement the activity refcount.

    When it reaches 0, stop the heartbeat timer and start an idle timer that logs after 30s
    of inactivity.
    """
    global _refcount, _heartbeat_timer
    if _refcount > 0:
        _refcount -= 1
    n = _active_reasons.get(reason, 0) - 1
    if n > 0:
        _active_reasons[reason] = n
    else:
        _active_reasons.pop(reason, None)
    if _refcount == 0 and _heartbeat_timer is not None:
        _heartbeat_timer.cancel()
        _heartbeat_timer = None
        _start_idle_timer()
