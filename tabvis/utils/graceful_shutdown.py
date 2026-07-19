"""Graceful shutdown flush/cleanup.

Installs process signal handlers (SIGINT / SIGTERM / SIGHUP), drains the cleanup registry,
executes SessionEnd hooks, flushes analytics, restores terminal modes, prints a resume hint, and
force-exits with a failsafe timer so a hung cleanup can never wedge the process.

Casing rule (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case. No dict-shaped
wire payloads round-trip here, so there are no wire keys to preserve. The raw ANSI/CSI/DEC/OSC
terminal escape sequences are protocol bytes kept verbatim.

Cyclic-group note (this module participates in the
``session_storage <-> file_history <-> tool_result_storage <-> graceful_shutdown`` cycle):
``get_current_session_title`` / ``session_id_exists`` come from ``session_storage`` (a cyclic
sibling). To keep this module import-standalone those are imported **lazily** inside
:func:`_print_resume_hint` — never at module top — and missing-sibling failures are swallowed
(the resume hint is best-effort). No top-level import of a cyclic sibling exists.

Substitutions / stubs:
- ``signal-exit`` (``onExit``) has no stdlib equivalent and is only used to pin the v4 emitter
  around a Bun bug — irrelevant under CPython; dropped.
- ``lodash-es/memoize`` (zero-arg) → a plain run-once guard.
- Terminal escape-sequence constants are inlined here verbatim as the small literals below.
- ``chalk.dim`` → inlined SGR 2 / 22 wrap.
- Datadog / first-party event-logger analytics are not implemented in this build;
  ``tabvis.analytics`` is a no-op sink in headless. Their shutdown flushers are local no-ops.
- SessionEnd hooks (``execute_session_end_hooks`` / ``get_session_end_hook_timeout_ms``) are
  imported lazily from ``tabvis.utils.hooks`` with a graceful fallback when that surface is absent.
- ``profile_report`` is imported lazily from ``tabvis.utils.startup_profiler`` with a no-op fallback.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any, Literal

from tabvis.bootstrap.state import (
    get_is_interactive,
    get_last_main_request_id,
    get_session_id,
    is_session_persistence_disabled,
)
from tabvis.utils.cleanup_registry import run_cleanup_functions
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.diag_logs import log_for_diagnostics_no_pii
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.log import log_error
from tabvis.utils.sleep import sleep

# ExitReason — the accepted set of shutdown reasons.
ExitReason = Literal[
    "clear",
    "resume",
    "logout",
    "prompt_input_exit",
    "other",
    "bypass_permissions_disabled",
]

# --- Terminal escape sequences (inlined verbatim as protocol bytes). ---
_DISABLE_MOUSE_TRACKING = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
_EXIT_ALT_SCREEN = "\x1b[?1049l"
_DISABLE_MODIFY_OTHER_KEYS = "\x1b[>4m"
_DISABLE_KITTY_KEYBOARD = "\x1b[<u"
_DFE = "\x1b[?1004l"  # Disable focus events (DECSET 1004)
_DBP = "\x1b[?2004l"  # Disable bracketed paste mode
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_ITERM2_PROGRESS = "\x1b]9;4;0;\x07"
_CLEAR_TAB_STATUS = "\x1b]21337;ClearStatus\x07"
_CLEAR_TERMINAL_TITLE = "\x1b]0;\x07"


def _supports_tab_status() -> bool:
    return False


def _wrap_for_multiplexer(sequence: str) -> str:
    return sequence


def _dim(text: str) -> str:
    """Inlined ``chalk.dim`` — SGR 2 (faint) … SGR 22 (normal intensity)."""
    return f"\x1b[2m{text}\x1b[22m"


def _write_sync(fd: int, data: str) -> None:
    os.write(fd, data.encode("utf-8"))


def _cleanup_terminal_modes() -> None:
    """Clean up terminal modes synchronously before process exit.

    Unconditionally sends all disable sequences (terminal detection is unreliable; the sequences
    are no-ops on terminals that don't support them; failing to disable leaves the terminal
    broken).
    """
    if not sys.stdout.isatty():
        return

    try:
        # Disable mouse tracking first so queued events don't leak to the shell.
        _write_sync(1, _DISABLE_MOUSE_TRACKING)
        # Exit alt screen so the resume hint + sequences below land on the main buffer.
        _write_sync(1, _EXIT_ALT_SCREEN)
        # Disable extended key reporting (send both — terminals ignore what they don't implement).
        _write_sync(1, _DISABLE_MODIFY_OTHER_KEYS)
        _write_sync(1, _DISABLE_KITTY_KEYBOARD)
        # Disable focus events (DECSET 1004).
        _write_sync(1, _DFE)
        # Disable bracketed paste mode.
        _write_sync(1, _DBP)
        # Show cursor.
        _write_sync(1, _SHOW_CURSOR)
        # Clear iTerm2 progress bar.
        _write_sync(1, _CLEAR_ITERM2_PROGRESS)
        # Clear tab status (OSC 21337) so a stale dot doesn't linger.
        if _supports_tab_status():
            _write_sync(1, _wrap_for_multiplexer(_CLEAR_TAB_STATUS))
        # Clear terminal title (respect TABVIS_DISABLE_TERMINAL_TITLE).
        if not is_env_truthy(os.environ.get("TABVIS_DISABLE_TERMINAL_TITLE")):
            if sys.platform == "win32":
                pass  # process.title = '' (no portable equivalent; title APIs are TUI-only)
            else:
                _write_sync(1, _CLEAR_TERMINAL_TITLE)
    except Exception:  # noqa: BLE001
        # Terminal may already be gone (e.g., SIGHUP after terminal close). Ignore.
        pass


_resume_hint_printed = False


def _print_resume_hint() -> None:
    """Print a hint about how to resume the session (interactive + persistence-enabled only)."""
    global _resume_hint_printed
    if _resume_hint_printed:
        return
    if (
        sys.stdout.isatty()
        and get_is_interactive()
        and not is_session_persistence_disabled()
    ):
        try:
            session_id = get_session_id()
            # Lazy cyclic-sibling import (session_storage may not exist yet). Best-effort: if the
            # sibling is absent, skip the hint.
            try:
                from tabvis.utils.session_storage import (  # noqa: PLC0415 (lazy cycle break)
                    get_current_session_title,
                    session_id_exists,
                )
            except Exception:  # noqa: BLE001
                return

            if not session_id_exists(session_id):
                return
            custom_title = get_current_session_title(session_id)

            if custom_title:
                escaped = custom_title.replace("\\", "\\\\").replace('"', '\\"')
                resume_arg = f'"{escaped}"'
            else:
                resume_arg = session_id

            _write_sync(
                1,
                _dim(f"\nResume this session with:\ntabvis --resume {resume_arg}\n"),
            )
            _resume_hint_printed = True
        except Exception:  # noqa: BLE001
            pass


def _force_exit(exit_code: int) -> None:
    """Force process exit, handling the case where the terminal is gone."""
    global _failsafe_timer
    if _failsafe_timer is not None:
        _failsafe_timer.cancel()
        _failsafe_timer = None
    try:
        sys.exit(exit_code)
    except SystemExit:
        raise
    except BaseException:
        # In tests, sys.exit may be patched to raise — re-raise so the test sees it.
        if os.environ.get("NODE_ENV") == "test":
            raise
        # Production fallback: SIGKILL never tries to flush anything.
        os.kill(os.getpid(), signal.SIGKILL)
    if os.environ.get("NODE_ENV") != "test":
        raise RuntimeError("unreachable")


_setup_done = False


def setup_graceful_shutdown() -> None:
    """Set up global signal handlers for graceful shutdown (run-once)."""
    global _setup_done
    if _setup_done:
        return
    _setup_done = True

    # NOTE: TS pins signal-exit v4 here to work around a Bun bug; irrelevant under CPython.

    def _on_sigint(_signum: int, _frame: Any) -> None:
        # In print mode, print.ts registers its own SIGINT handler; skip here to avoid racing.
        if "-p" in sys.argv or "--print" in sys.argv:
            return
        log_for_diagnostics_no_pii("info", "shutdown_signal", {"signal": "SIGINT"})
        _schedule(graceful_shutdown(0))

    def _on_sigterm(_signum: int, _frame: Any) -> None:
        log_for_diagnostics_no_pii("info", "shutdown_signal", {"signal": "SIGTERM"})
        _schedule(graceful_shutdown(143))  # 128 + 15

    def _on_sighup(_signum: int, _frame: Any) -> None:
        log_for_diagnostics_no_pii("info", "shutdown_signal", {"signal": "SIGHUP"})
        _schedule(graceful_shutdown(129))  # 128 + 1

    try:
        signal.signal(signal.SIGINT, _on_sigint)
        signal.signal(signal.SIGTERM, _on_sigterm)
        if sys.platform != "win32":
            signal.signal(signal.SIGHUP, _on_sighup)
    except (ValueError, OSError):
        # signal.signal only works on the main thread; ignore when not available.
        pass

    # NOTE: the macOS orphan-detection interval + uncaught-exception / unhandled-rejection
    # observers attach to the Node event loop / process; they have no headless equivalent here.


def _schedule(coro: Any) -> None:
    """Schedule the shutdown coroutine on the running loop, or run it to completion if none."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        task.add_done_callback(_swallow_task_error)
    else:
        try:
            asyncio.run(coro)
        except Exception as error:  # noqa: BLE001
            log_error(error)


def _swallow_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:  # noqa: BLE001
        pass


def graceful_shutdown_sync(
    exit_code: int = 0,
    reason: ExitReason = "other",
    options: dict[str, Any] | None = None,
) -> None:
    """Synchronous shutdown entry: sets the exit code and kicks off the async drain."""
    global _pending_shutdown

    async def _run() -> None:
        try:
            await graceful_shutdown(exit_code, reason, options)
        except Exception as error:  # noqa: BLE001
            log_for_debugging(f"Graceful shutdown failed: {error}", {"level": "error"})
            _cleanup_terminal_modes()
            _print_resume_hint()
            try:
                _force_exit(exit_code)
            except Exception:  # noqa: BLE001
                # forceExit re-raises in test mode; swallow to prevent an unhandled rejection.
                pass

    _pending_shutdown = asyncio.ensure_future(_run()) if _has_running_loop() else None
    if _pending_shutdown is None:
        _schedule(_run())


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


_shutdown_in_progress = False
_failsafe_timer: asyncio.TimerHandle | None = None
_pending_shutdown: asyncio.Future | None = None


def is_shutting_down() -> bool:
    """Check if graceful shutdown is in progress."""
    return _shutdown_in_progress


def reset_shutdown_state() -> None:
    """Reset shutdown state — only for use in tests."""
    global _shutdown_in_progress, _resume_hint_printed, _failsafe_timer, _pending_shutdown
    _shutdown_in_progress = False
    _resume_hint_printed = False
    if _failsafe_timer is not None:
        _failsafe_timer.cancel()
        _failsafe_timer = None
    _pending_shutdown = None


def get_pending_shutdown_for_testing() -> asyncio.Future | None:
    """Return the in-flight shutdown future, if any (tests await before restoring mocks)."""
    return _pending_shutdown


class _CleanupTimeoutError(Exception):
    def __init__(self) -> None:
        super().__init__("Cleanup timeout")


def _get_session_end_hook_timeout_ms() -> int:
    """Lazily resolve the SessionEnd hook timeout budget from the hooks surface."""
    try:
        from tabvis.utils.hooks import (  # noqa: PLC0415
            get_session_end_hook_timeout_ms,
        )

        return get_session_end_hook_timeout_ms()
    except Exception:  # noqa: BLE001
        # (TABVIS_SESSIONEND_HOOKS_TIMEOUT_MS default).
        return 1500


async def _execute_session_end_hooks(
    reason: ExitReason, options: dict[str, Any], timeout_ms: int
) -> None:
    """Lazily execute SessionEnd hooks; no-op fallback when the hooks surface is absent."""
    try:
        from tabvis.utils.hooks import execute_session_end_hooks  # noqa: PLC0415

        await execute_session_end_hooks(
            reason, {**options, "timeoutMs": timeout_ms}
        )
    except Exception:  # noqa: BLE001
        pass


async def _shutdown_datadog() -> None:
    """No-op: datadog analytics is not implemented in this build."""
    return None


async def _shutdown_1p_event_logging() -> None:
    """No-op: first-party event logging is not implemented in this build."""
    return None


def _profile_report() -> None:
    """Best-effort startup-perf report; no-op when the profiler surface is absent."""
    try:
        from tabvis.utils.startup_profiler import profile_report  # noqa: PLC0415

        profile_report()
    except Exception:  # noqa: BLE001
        pass


async def graceful_shutdown(
    exit_code: int = 0,
    reason: ExitReason = "other",
    options: dict[str, Any] | None = None,
) -> None:
    """Graceful shutdown that drains the event loop, runs cleanup + hooks, flushes, then exits."""
    global _shutdown_in_progress, _failsafe_timer
    if _shutdown_in_progress:
        return
    _shutdown_in_progress = True
    options = options or {}

    # Resolve the SessionEnd hook budget before arming the failsafe so the failsafe scales with it.
    session_end_timeout_ms = _get_session_end_hook_timeout_ms()

    # Failsafe: guarantee process exits even if cleanup hangs. Budget = max(5s, hook + 3.5s).
    def _failsafe() -> None:
        _cleanup_terminal_modes()
        _print_resume_hint()
        _force_exit(exit_code)

    try:
        loop = asyncio.get_running_loop()
        _failsafe_timer = loop.call_later(
            max(5000, session_end_timeout_ms + 3500) / 1000, _failsafe
        )
    except RuntimeError:
        _failsafe_timer = None

    # Exit alt screen + print resume hint FIRST so it's visible even if killed during cleanup.
    _cleanup_terminal_modes()
    _print_resume_hint()

    # Flush session data first (most critical). Race the cleanup against a 2s timeout.
    try:
        await asyncio.wait_for(_run_cleanup_safe(), timeout=2.0)
    except (TimeoutError, _CleanupTimeoutError, Exception):  # noqa: BLE001
        pass

    # Execute SessionEnd hooks (bounded by the budget).
    try:
        await _execute_session_end_hooks(reason, options, session_end_timeout_ms)
    except Exception:  # noqa: BLE001
        pass

    # Log startup perf before analytics shutdown flushes/cancels timers.
    try:
        _profile_report()
    except Exception:  # noqa: BLE001
        pass

    # Signal to inference that this session's cache can be evicted (before analytics flush).
    last_request_id = get_last_main_request_id()
    if last_request_id:
        pass

    # Flush analytics — capped at 500ms.
    try:
        await asyncio.wait_for(
            asyncio.gather(_shutdown_1p_event_logging(), _shutdown_datadog()),
            timeout=0.5,
        )
    except Exception:  # noqa: BLE001
        # sleep(500) parity is implicit in the timeout; ignore analytics shutdown errors.
        await sleep(0)

    final_message = options.get("finalMessage")
    if final_message:
        try:
            _write_sync(2, final_message + "\n")
        except Exception:  # noqa: BLE001
            pass

    _force_exit(exit_code)


async def _run_cleanup_safe() -> None:
    try:
        await run_cleanup_functions()
    except Exception:  # noqa: BLE001
        pass
