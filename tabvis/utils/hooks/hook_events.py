"""Hook event broadcast system

A generic event system, separate from the main message stream. Handlers register to receive hook
execution events (``started`` / ``progress`` / ``response``) and decide what to do with them (e.g.
convert to SDK messages, log, etc.).

Module-level mutable state mirrors the TS module-scope closures (a single registered handler, a
bounded pending-event buffer, and an "all events enabled" flag).

Casing: Python identifiers snake_case; the event dict wire keys (``hookId``/``hookName``/
``hookEvent``/``exitCode``) stay camelCase because they round-trip to the SDK event consumer.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable
from typing import Any

from tabvis.utils.debug import log_for_debugging

# The full set of hook event names emitted to SDK consumers.
HOOK_EVENTS: tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "UserPromptSubmit",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
)

# Hook events that are always emitted regardless of the includeHookEvents option. These are
# low-noise lifecycle events that were in the original allowlist and are backwards-compatible.
ALWAYS_EMITTED_HOOK_EVENTS: tuple[str, ...] = ("SessionStart", "Setup")

MAX_PENDING_EVENTS = 100

# A hook execution event is one of the 'started' / 'progress' / 'response' dict shapes; kept as a
# plain dict (wire keys camelCase) so it round-trips verbatim to the SDK event consumer.
HookExecutionEvent = dict[str, Any]
HookEventHandler = Callable[[HookExecutionEvent], None]

_pending_events: list[HookExecutionEvent] = []
_event_handler: HookEventHandler | None = None
_all_hook_events_enabled = False


def register_hook_event_handler(handler: HookEventHandler | None) -> None:
    """Register or clear the event handler, flushing buffered events when registered."""
    global _event_handler
    _event_handler = handler
    if handler and _pending_events:
        drained = _pending_events[:]
        _pending_events.clear()
        for event in drained:
            handler(event)


def _emit(event: HookExecutionEvent) -> None:
    """Deliver to the handler, or buffer (bounded FIFO) when none is registered."""
    if _event_handler:
        _event_handler(event)
    else:
        _pending_events.append(event)
        if len(_pending_events) > MAX_PENDING_EVENTS:
            _pending_events.pop(0)


def _should_emit(hook_event: str) -> bool:
    if hook_event in ALWAYS_EMITTED_HOOK_EVENTS:
        return True
    return _all_hook_events_enabled and hook_event in HOOK_EVENTS


def emit_hook_started(hook_id: str, hook_name: str, hook_event: str) -> None:
    """Emit the hook started."""
    if not _should_emit(hook_event):
        return
    _emit(
        {
            "type": "started",
            "hookId": hook_id,
            "hookName": hook_name,
            "hookEvent": hook_event,
        }
    )


def emit_hook_progress(data: dict[str, Any]) -> None:
    """``Data`` carries hookId/hookName/hookEvent/stdout/stderr/output."""
    if not _should_emit(data["hookEvent"]):
        return
    _emit({"type": "progress", **data})


def start_hook_progress_interval(
    *,
    hook_id: str,
    hook_name: str,
    hook_event: str,
    get_output: Callable[[], Awaitable[dict[str, str]]],
    interval_ms: int | None = None,
) -> Callable[[], None]:
    """Poll ``get_output`` on an interval and emit progress when the output changes.

    The TS version uses ``setInterval`` + an unref'd timer;
    here a daemon :class:`threading.Timer` chain stands in (the polled ``get_output`` coroutine is
    driven via a fresh event loop per tick — this helper is for background progress reporting and is
    not on the hot path). Returns a cancel callable.
    """
    if not _should_emit(hook_event):
        return lambda: None

    import asyncio

    state: dict[str, Any] = {"last": "", "timer": None, "stopped": False}
    period = (interval_ms if interval_ms is not None else 1000) / 1000

    def tick() -> None:
        if state["stopped"]:
            return
        try:
            result = asyncio.run(get_output())
            output = result.get("output", "")
            if output != state["last"]:
                state["last"] = output
                emit_hook_progress(
                    {
                        "hookId": hook_id,
                        "hookName": hook_name,
                        "hookEvent": hook_event,
                        "stdout": result.get("stdout", ""),
                        "stderr": result.get("stderr", ""),
                        "output": output,
                    }
                )
        except Exception:  # noqa: BLE001 - background poller must not raise into the timer
            pass
        if not state["stopped"]:
            timer = threading.Timer(period, tick)
            timer.daemon = True
            state["timer"] = timer
            timer.start()

    first = threading.Timer(period, tick)
    first.daemon = True
    state["timer"] = first
    first.start()

    def cancel() -> None:
        state["stopped"] = True
        timer = state["timer"]
        if timer is not None:
            timer.cancel()

    return cancel


def emit_hook_response(data: dict[str, Any]) -> None:
    """Emit the hook response.

    Always logs the full hook output to the debug log (verbose-mode debugging), then emits the
    ``response`` event when the event type is allowed.
    """
    output_to_log = data.get("stdout") or data.get("stderr") or data.get("output")
    if output_to_log:
        log_for_debugging(
            f"Hook {data.get('hookName')} ({data.get('hookEvent')}) "
            f"{data.get('outcome')}:\n{output_to_log}"
        )

    if not _should_emit(data["hookEvent"]):
        return

    _emit({"type": "response", **data})


def set_all_hook_events_enabled(enabled: bool) -> None:
    """Enable emission of all hook event types (beyond SessionStart and Setup).

    Called when the SDK ``includeHookEvents`` option is set or when running in TABVIS_REMOTE mode.
    Set the all hook events enabled.
    """
    global _all_hook_events_enabled
    _all_hook_events_enabled = enabled


def clear_hook_event_state() -> None:
    """Reset all module state."""
    global _event_handler, _all_hook_events_enabled
    _event_handler = None
    _pending_events.clear()
    _all_hook_events_enabled = False
