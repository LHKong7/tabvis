"""Settings-file change detection

Watches the user / project / local settings files (and the ``managed-settings.d/`` policy drop-in
directory) for on-disk changes, and polls the OS-level MDM (registry / plist) settings, fanning out
a single ``settingsChanged`` signal per detected change after resetting the settings cache.

Design notes
------------
* **Injectable watcher:** there is no stdlib/maintained drop-in for a native FS-event watcher with
  ``awaitWriteFinish`` debouncing. The *decision logic* is
  implemented in full (:func:`get_watch_targets`, :func:`get_source_for_path`, :func:`handle_change`,
  :func:`handle_delete`, :func:`handle_add`, :func:`fan_out`, the MDM poll, :func:`dispose`,
  :func:`notify_change`). The actual filesystem-event backend is left as a single seam: a module-level
  ``_watcher_factory`` hook that :func:`initialize` calls with the resolved watch targets + event
  callbacks. With no factory installed (headless default) initialize is a structural no-op past the
  MDM poll — matching "watchers stubbed" in the settings ledger. Tests (and a future native backend)
  install a factory to drive ``change``/``add``/``unlink`` events through the existing handlers.
* **setInterval / setTimeout -> :class:`threading.Timer`** (same pattern as
  ``tabvis/utils/session_activity.py``): the MDM poll re-arms itself (``setInterval``); the deletion
  grace timers are one-shot (``setTimeout``). ``mdmPollTimer.unref()`` (don't keep the process alive)
  -> ``Timer.daemon = True``.
* **``void executeConfigChangeHooks(...).then(...)``** (fire-and-forget async) -> a daemon thread that
  runs the (async) hook execution to completion, then applies the same blocking check.
* The config-change hook surface (``execute_config_change_hooks`` / ``has_blocking_result`` /
  ``ConfigChangeSource``) is defined locally in this module: no config-change hooks are wired in
  this build, so execution returns an empty result list and ``has_blocking_result`` is
  ``any(r['blocked'])``.

Casing: Python identifiers snake_case; the wire :data:`SettingSource` values and the
``ConfigChangeSource`` strings (``user_settings`` etc.) are kept verbatim — they round-trip to hook
JSON payloads.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from typing import Any, Literal

from ...bootstrap.state import get_is_remote_mode
from ..cleanup_registry import register_cleanup
from ..debug import log_for_debugging
from ..errors import get_error_message
from ..signal import create_signal
from ..slow_operations import json_stringify
from .constants import SETTING_SOURCES, SettingSource
from .internal_writes import clear_internal_writes, consume_internal_write
from .managed_path import get_managed_settings_drop_in_dir
from .mdm.settings import (
    get_hkcu_settings,
    get_mdm_settings,
    refresh_mdm_settings,
    set_mdm_settings_cache,
)
from .settings import get_settings_file_path_for_source
from .settings_cache import reset_settings_cache

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

ConfigChangeSource = Literal[
    "user_settings",
    "project_settings",
    "local_settings",
    "policy_settings",
    "skills",
]

async def _execute_config_change_hooks(
    source: ConfigChangeSource,
    file_path: str | None = None,
    timeout_ms: int | None = None,
) -> list[dict[str, Any]]:
    """No ConfigChange hooks are wired in this build -> empty result list.

    Matches the behaviour of having no configured config-change hooks (and the policy-source
    rule that blocking is never honoured — an empty list is never blocking).
    """
    return []


def _has_blocking_result(results: list[dict[str, Any]]) -> bool:
    """Return True if any result is marked ``blocked``."""
    return any(r.get("blocked") for r in results)


# ---------------------------------------------------------------------------
# Timing constants
# ---------------------------------------------------------------------------

# Time in milliseconds to wait for file writes to stabilize before processing.
# This helps avoid processing partial writes or rapid successive changes.
FILE_STABILITY_THRESHOLD_MS = 1000

# Polling interval in milliseconds for checking file stability.
# Used by chokidar's awaitWriteFinish option. Must be lower than FILE_STABILITY_THRESHOLD_MS.
FILE_STABILITY_POLL_INTERVAL_MS = 500

# Time window in milliseconds to consider a file change as internal. If a file change occurs within
# this window after mark_internal_write() is called, it's assumed to be from Tabvis itself and won't
# trigger a notification.
INTERNAL_WRITE_WINDOW_MS = 5000

# Poll interval for MDM settings (registry/plist) changes. These can't be watched via filesystem
# events, so we poll periodically.
MDM_POLL_INTERVAL_MS = 30 * 60 * 1000  # 30 minutes

# Grace period in milliseconds before processing a settings file deletion. Handles the common
# delete-and-recreate pattern during auto-updates or when another session starts up. If an ``add``
# or ``change`` event fires within this window (file was recreated), the deletion is cancelled and
# treated as a change.
#
# Must exceed chokidar's awaitWriteFinish delay (stabilityThreshold + pollInterval) so the grace
# window outlasts the write stability check on the recreated file.
DELETION_GRACE_MS = (
    FILE_STABILITY_THRESHOLD_MS + FILE_STABILITY_POLL_INTERVAL_MS + 200
)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class Watcher:
    """Minimal protocol of the FS watcher backend (the slice of chokidar this module uses).

    A backend must dispatch ``change`` / ``unlink`` / ``add`` events (each with the changed path)
    to the registered handlers and provide a :meth:`close` that settles when watching has stopped.
    No native backend ships in the headless skeleton; tests install one via
    :func:`set_watcher_factory`.
    """

    def on(self, event: str, handler: Callable[[str], None]) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def close(self) -> Any:  # pragma: no cover - protocol
        raise NotImplementedError


# Factory: (dirs, settings_files, drop_in_dir, overrides) -> Watcher. ``None`` => no FS watching
# (structural no-op past the MDM poll), faithful to "watchers stubbed" in the skeleton.
WatcherFactory = Callable[
    [list[str], set[str], str | None, dict[str, Any]], Watcher
]

_watcher_factory: WatcherFactory | None = None


def set_watcher_factory(factory: WatcherFactory | None) -> None:
    """Install (or clear) the FS-watcher backend used by :func:`initialize` (test/native seam)."""
    global _watcher_factory
    _watcher_factory = factory


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_watcher: Watcher | None = None
_mdm_poll_timer: threading.Timer | None = None
_last_mdm_snapshot: str | None = None
_initialized = False
_disposed = False
_pending_deletions: dict[str, threading.Timer] = {}
_settings_changed = create_signal()

# Test overrides for timing constants (stabilityThreshold / pollInterval / mdmPollInterval /
# deletionGrace), in milliseconds.
_test_overrides: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Initialize / dispose
# ---------------------------------------------------------------------------


async def initialize() -> None:
    """Initialize file watching.

    No-op in remote mode or once already initialized/disposed. Starts the MDM poll, registers the
    cleanup, resolves the watch targets, and (if a watcher factory is installed) starts the FS
    watcher. Without a factory the FS-watch step is skipped — the headless default.
    """
    global _initialized, _watcher
    if get_is_remote_mode():
        return
    if _initialized or _disposed:
        return
    _initialized = True

    # Start MDM poll for registry/plist changes (independent of filesystem watching).
    start_mdm_poll()

    # Register cleanup to properly dispose during graceful shutdown.
    register_cleanup(dispose)

    targets = await get_watch_targets()
    dirs = targets["dirs"]
    settings_files = targets["settingsFiles"]
    drop_in_dir = targets["dropInDir"]
    if _disposed:  # dispose() ran during the await
        return
    if len(dirs) == 0:
        return

    log_for_debugging(
        f"Watching for changes in setting files {', '.join(sorted(settings_files))}..."
        + (f" and drop-in directory {drop_in_dir}" if drop_in_dir else "")
    )

    if _watcher_factory is None:
        return

    _watcher = _watcher_factory(
        dirs,
        settings_files,
        drop_in_dir,
        {
            "stabilityThreshold": (_test_overrides or {}).get("stabilityThreshold")
            or FILE_STABILITY_THRESHOLD_MS,
            "pollInterval": (_test_overrides or {}).get("pollInterval")
            or FILE_STABILITY_POLL_INTERVAL_MS,
        },
    )

    _watcher.on("change", handle_change)
    _watcher.on("unlink", handle_delete)
    _watcher.on("add", handle_add)


def dispose() -> Any:
    """Clean up file watcher.

    Returns the watcher-close awaitable/result so callers that must wait for the watcher to fully
    stop before removing the watched directory can await it. Fire-and-forget remains valid where
    timing doesn't matter.
    """
    global _watcher, _mdm_poll_timer, _last_mdm_snapshot, _disposed
    _disposed = True
    if _mdm_poll_timer is not None:
        _mdm_poll_timer.cancel()
        _mdm_poll_timer = None
    for timer in _pending_deletions.values():
        timer.cancel()
    _pending_deletions.clear()
    _last_mdm_snapshot = None
    clear_internal_writes()
    _settings_changed.clear()
    w = _watcher
    _watcher = None
    return w.close() if w is not None else None


# Subscribe to settings changes.
subscribe = _settings_changed.subscribe


# ---------------------------------------------------------------------------
# Watch-target resolution
# ---------------------------------------------------------------------------


async def get_watch_targets() -> dict[str, Any]:
    """Collect settings file paths + their dedup'd parent dirs to watch (``getWatchTargets``).

    Returns ``{"dirs": [...], "settingsFiles": {...}, "dropInDir": str | None}`` (wire-ish keys kept
    to mirror the TS object literal). Includes ALL potential settings files for any watched directory
    (not just those existing now) so newly-created files are also detected.
    """
    # Map from directory to all potential settings files in that directory.
    dir_to_settings_files: dict[str, set[str]] = {}
    dirs_with_existing_files: set[str] = set()

    for source in SETTING_SOURCES:
        # Skip flagSettings — provided via CLI, won't change during the session (and may be temp
        # files in $TMPDIR which can contain special files that hang the watcher).
        if source == "flagSettings":
            continue
        path = get_settings_file_path_for_source(source)
        if not path:
            continue

        directory = os.path.dirname(path)

        # Track all potential settings files in each directory.
        dir_to_settings_files.setdefault(directory, set()).add(path)

        # Check if file exists — only watch directories that have at least one existing file.
        try:
            if os.path.isfile(path):
                dirs_with_existing_files.add(directory)
        except OSError:
            # File doesn't exist / unreadable — that's fine.
            pass

    # For watched directories, include ALL potential settings file paths so files created after
    # init are also detected.
    settings_files: set[str] = set()
    for directory in dirs_with_existing_files:
        files_in_dir = dir_to_settings_files.get(directory)
        if files_in_dir:
            settings_files.update(files_in_dir)

    # Also watch the managed-settings.d/ drop-in directory for policy fragments. Any .json file
    # inside it maps to the 'policySettings' source.
    drop_in_dir: str | None = None
    managed_drop_in = get_managed_settings_drop_in_dir()
    try:
        if os.path.isdir(managed_drop_in):
            dirs_with_existing_files.add(managed_drop_in)
            drop_in_dir = managed_drop_in
    except OSError:
        # Drop-in directory doesn't exist — that's fine.
        pass

    return {
        "dirs": list(dirs_with_existing_files),
        "settingsFiles": settings_files,
        "dropInDir": drop_in_dir,
    }


def setting_source_to_config_change_source(source: SettingSource) -> ConfigChangeSource:
    """Map a :data:`SettingSource` to its :data:`ConfigChangeSource` (``settingSourceToConfigChangeSource``)."""
    if source == "userSettings":
        return "user_settings"
    if source == "projectSettings":
        return "project_settings"
    if source == "localSettings":
        return "local_settings"
    # flagSettings / policySettings -> policy_settings.
    return "policy_settings"


# ---------------------------------------------------------------------------
# FS event handlers
# ---------------------------------------------------------------------------


def handle_change(path: str) -> None:
    """Handle a settings-file change event."""
    source = get_source_for_path(path)
    if not source:
        return

    # If a deletion was pending for this path (delete-and-recreate pattern), cancel it — we'll
    # process this as a change instead.
    pending_timer = _pending_deletions.get(path)
    if pending_timer is not None:
        pending_timer.cancel()
        _pending_deletions.pop(path, None)
        log_for_debugging(f"Cancelled pending deletion of {path} — file was recreated")

    # Check if this was an internal write.
    if consume_internal_write(path, INTERNAL_WRITE_WINDOW_MS):
        return

    log_for_debugging(f"Detected change to {path}")

    # Fire ConfigChange hook first — if blocked, skip applying the change to the session.
    _run_config_change_hooks_then(
        setting_source_to_config_change_source(source),
        path,
        source,
        blocked_message=f"ConfigChange hook blocked change to {path}",
    )


def handle_add(path: str) -> None:
    """Handle a settings file being re-added after deletion or replacement."""
    source = get_source_for_path(path)
    if not source:
        return

    # Cancel any pending deletion — the file is back.
    pending_timer = _pending_deletions.get(path)
    if pending_timer is not None:
        pending_timer.cancel()
        _pending_deletions.pop(path, None)
        log_for_debugging(f"Cancelled pending deletion of {path} — file was re-added")

    # Treat as a change (re-read settings).
    handle_change(path)


def handle_delete(path: str) -> None:
    """Handle a file being deleted, with a delete-and-recreate grace window (``handleDelete``)."""
    source = get_source_for_path(path)
    if not source:
        return

    log_for_debugging(f"Detected deletion of {path}")

    # If there's already a pending deletion for this path, let it run.
    if path in _pending_deletions:
        return

    grace_ms = (_test_overrides or {}).get("deletionGrace") or DELETION_GRACE_MS

    def _fire(p: str = path, src: SettingSource = source) -> None:
        _pending_deletions.pop(p, None)
        # Fire ConfigChange hook first — if blocked, skip applying the deletion.
        _run_config_change_hooks_then(
            setting_source_to_config_change_source(src),
            p,
            src,
            blocked_message=f"ConfigChange hook blocked deletion of {p}",
        )

    timer = threading.Timer(grace_ms / 1000, _fire)
    timer.daemon = True
    _pending_deletions[path] = timer
    timer.start()


def get_source_for_path(path: str) -> SettingSource | None:
    """Resolve which :data:`SettingSource` a changed path belongs to."""
    # Normalize path (chokidar uses forward slashes on Windows).
    normalized_path = os.path.normpath(path)

    # Check if the path is inside the managed-settings.d/ drop-in directory.
    drop_in_dir = get_managed_settings_drop_in_dir()
    if normalized_path.startswith(drop_in_dir + os.sep):
        return "policySettings"

    for source in SETTING_SOURCES:
        if get_settings_file_path_for_source(source) == normalized_path:
            return source
    return None


# ---------------------------------------------------------------------------
# Config-change hook orchestration (the ``void hooks.then(...)`` fire-and-forget)
# ---------------------------------------------------------------------------


def _run_config_change_hooks_then(
    config_source: ConfigChangeSource,
    path: str,
    source: SettingSource,
    *,
    blocked_message: str,
) -> None:
    """Run the (async) ConfigChange hooks off-thread, then fan out unless blocked.

        return; fanOut(source) })`` pattern. The hook run is async; the headless fallback resolves
    immediately. A daemon thread keeps this fire-and-forget like the TS ``void``.
    """

    def _worker() -> None:
        try:
            results = asyncio.run(_execute_config_change_hooks(config_source, path))
        except Exception as error:  # noqa: BLE001 — hook failures must not crash the watcher.
            log_for_debugging(f"ConfigChange hook error: {get_error_message(error)}")
            results = []
        if _has_blocking_result(results):
            log_for_debugging(blocked_message)
            return
        fan_out(source)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# MDM polling
# ---------------------------------------------------------------------------


def start_mdm_poll() -> None:
    """Start polling for MDM settings changes (registry/plist).

    Captures an initial snapshot (admin MDM + user-writable HKCU) and compares each tick.
    """
    global _last_mdm_snapshot, _mdm_poll_timer

    initial = get_mdm_settings()
    initial_hkcu = get_hkcu_settings()
    _last_mdm_snapshot = json_stringify(
        {"mdm": initial.settings, "hkcu": initial_hkcu.settings}
    )

    interval_ms = (_test_overrides or {}).get("mdmPollInterval") or MDM_POLL_INTERVAL_MS

    def _tick() -> None:
        global _mdm_poll_timer
        if _disposed:
            return
        _mdm_poll_check()
        # Re-arm to emulate setInterval (only while still the active timer / not disposed).
        if _disposed:
            return
        new_timer = threading.Timer(interval_ms / 1000, _tick)
        new_timer.daemon = True
        _mdm_poll_timer = new_timer
        new_timer.start()

    _mdm_poll_timer = threading.Timer(interval_ms / 1000, _tick)
    # Don't let the timer keep the process alive).
    _mdm_poll_timer.daemon = True
    _mdm_poll_timer.start()


def _mdm_poll_check() -> None:
    """One MDM poll tick: refresh, compare snapshot, apply + fan out on change.

    Set the interval.
    """
    global _last_mdm_snapshot
    try:
        refreshed = asyncio.run(refresh_mdm_settings())
        if _disposed:
            return
        current = refreshed["mdm"]
        current_hkcu = refreshed["hkcu"]

        current_snapshot = json_stringify(
            {"mdm": current.settings, "hkcu": current_hkcu.settings}
        )

        if current_snapshot != _last_mdm_snapshot:
            _last_mdm_snapshot = current_snapshot
            # Update the cache so sync readers pick up new values.
            set_mdm_settings_cache(current, current_hkcu)
            log_for_debugging("Detected MDM settings change via poll")
            fan_out("policySettings")
    except Exception as error:  # noqa: BLE001 — poll errors are logged, not fatal.
        log_for_debugging(f"MDM poll error: {get_error_message(error)}")


# ---------------------------------------------------------------------------
# Fan-out / programmatic notify
# ---------------------------------------------------------------------------


def fan_out(source: SettingSource) -> None:
    """Reset the settings cache, then notify all listeners.

    The cache reset MUST happen here (single producer), not in each listener, so one notification =
    one disk reload.
    """
    reset_settings_cache()
    _settings_changed.emit(source)


def notify_change(source: SettingSource) -> None:
    """Manually notify listeners of a settings change.

    Used for programmatic settings changes that don't involve filesystem changes.
    """
    log_for_debugging(f"Programmatic settings change notification for {source}")
    fan_out(source)


# ---------------------------------------------------------------------------
# Test-only reset
# ---------------------------------------------------------------------------


def reset_for_testing(overrides: dict[str, Any] | None = None) -> Any:
    """Reset internal state for testing only.

    Allows re-initialization after :func:`dispose`. Optionally accepts timing overrides
    (``stabilityThreshold`` / ``pollInterval`` / ``mdmPollInterval`` / ``deletionGrace``, all ms)
    for faster test execution. Returns the watcher-close result so test teardown can await it
    before removing the watched directory.
    """
    global _mdm_poll_timer, _last_mdm_snapshot, _initialized, _disposed, _test_overrides, _watcher
    if _mdm_poll_timer is not None:
        _mdm_poll_timer.cancel()
        _mdm_poll_timer = None
    for timer in _pending_deletions.values():
        timer.cancel()
    _pending_deletions.clear()
    _last_mdm_snapshot = None
    _initialized = False
    _disposed = False
    _test_overrides = overrides
    w = _watcher
    _watcher = None
    return w.close() if w is not None else None


# Object-literal export mirroring the TS ``settingsChangeDetector`` aggregate.
settings_change_detector = {
    "initialize": initialize,
    "dispose": dispose,
    "subscribe": subscribe,
    "notifyChange": notify_change,
    "resetForTesting": reset_for_testing,
}
