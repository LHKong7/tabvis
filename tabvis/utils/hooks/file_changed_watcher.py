"""FileChanged / CwdChanged hook watcher

Watches files named in ``FileChanged`` hook matchers (pipe-separated filenames in ``cwd``, plus
dynamic paths emitted by hook output) and runs ``executeFileChangedHooks`` on change/add/unlink.
On cwd change it clears env files and runs ``executeCwdChangedHooks``.

chokidar has no stdlib / maintained drop-in for its native FS-event watcher with ``awaitWriteFinish``
debouncing, so — exactly as ``tabvis/utils/settings/change_detector.py`` does — the *decision logic*
(path resolution, dedup, restart, dispatch) is implemented in full while the native watcher sits behind
an injectable :func:`set_watcher_factory` seam. With
no factory installed the FS-watch step is skipped (the headless default); ``executeCwdChangedHooks``
still runs on cwd change. ``executeFileChangedHooks`` / ``executeCwdChangedHooks`` /
``getHooksConfigFromSnapshot`` are hooks-core siblings implemented in parallel — imported lazily.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import get_error_message as error_message

# ----------------------------------------------------------------------------------------------
# ----------------------------------------------------------------------------------------------


class Watcher:
    """Minimal protocol of the FS watcher backend (the slice of chokidar this module uses).

    A backend must dispatch ``change`` / ``add`` / ``unlink`` events (each with the changed path) to
    the registered handlers and provide a :meth:`close` that settles when watching has stopped. No
    native backend ships in the headless skeleton; tests install one via :func:`set_watcher_factory`.
    """

    def on(self, event: str, handler: Callable[[str], None]) -> None:  # pragma: no cover - protocol
        raise NotImplementedError

    def close(self) -> Any:  # pragma: no cover - protocol
        raise NotImplementedError


# Factory: (paths) -> Watcher. ``None`` => no FS watching (the headless default).
WatcherFactory = Callable[[list[str]], Watcher]

_watcher_factory: WatcherFactory | None = None


def set_watcher_factory(factory: WatcherFactory | None) -> None:
    """Install (or clear) the FS-watcher backend used by :func:`start_watching` (test/native seam)."""
    global _watcher_factory
    _watcher_factory = factory


# ----------------------------------------------------------------------------------------------
# Module-level state (mirrors the TS file-scoped let bindings)
# ----------------------------------------------------------------------------------------------

_watcher: Watcher | None = None
_current_cwd: str = ""
_dynamic_watch_paths: list[str] = []
_dynamic_watch_paths_sorted: list[str] = []
_initialized = False
_has_env_hooks = False
_notify_callback: Callable[[str, bool], None] | None = None


def set_env_hook_notifier(cb: Callable[[str, bool], None] | None) -> None:
    """Set the callback used to surface hook system messages / errors (``(text, is_error)``)."""
    global _notify_callback
    _notify_callback = cb


def _get_hooks_config_from_snapshot() -> dict[str, Any] | None:
    """Lazily read the hooks config snapshot (hooks-core sibling implemented in parallel)."""
    from tabvis.utils.hooks.hooks_config_snapshot import (  # noqa: PLC0415
        get_hooks_config_from_snapshot,
    )

    return get_hooks_config_from_snapshot()


def initialize_file_changed_watcher(cwd: str) -> None:
    """Initialize the FileChanged watcher for ``cwd``."""
    global _initialized, _current_cwd, _has_env_hooks
    if _initialized:
        return
    _initialized = True
    _current_cwd = cwd

    config = _get_hooks_config_from_snapshot()
    _has_env_hooks = _count(config, "CwdChanged") > 0 or _count(config, "FileChanged") > 0

    if _has_env_hooks:
        register_cleanup(lambda: _async_dispose())

    paths = _resolve_watch_paths(config)
    if len(paths) == 0:
        return

    _start_watching(paths)


def _count(config: dict[str, Any] | None, key: str) -> int:
    """``config?.[key]?.length ?? 0``."""
    if not config:
        return 0
    matchers = config.get(key)
    return len(matchers) if matchers else 0


def _resolve_watch_paths(config: dict[str, Any] | None = None) -> list[str]:
    """Resolve the set of paths to watch (static matcher paths + dynamic hook-output paths)."""
    cfg = config if config is not None else _get_hooks_config_from_snapshot()
    matchers = (cfg.get("FileChanged") if cfg else None) or []

    # Matcher field: filenames to watch in cwd, pipe-separated (e.g. ".envrc|.env").
    static_paths: list[str] = []
    for m in matchers:
        matcher = m.get("matcher")
        if not matcher:
            continue
        for name in (s.strip() for s in matcher.split("|")):
            if not name:
                continue
            static_paths.append(
                name if os.path.isabs(name) else os.path.join(_current_cwd, name)
            )

    # Combine static matcher paths with dynamic paths from hook output (dedup, order preserved).
    seen: dict[str, None] = {}
    for p in [*static_paths, *_dynamic_watch_paths]:
        seen.setdefault(p, None)
    return list(seen.keys())


def _start_watching(paths: list[str]) -> None:
    """Start the FS watcher over ``paths`` (no-op without an installed factory)."""
    global _watcher
    log_for_debugging(f"FileChanged: watching {len(paths)} paths")
    if _watcher_factory is None:
        return
    _watcher = _watcher_factory(paths)
    _watcher.on("change", lambda p: _handle_file_event(p, "change"))
    _watcher.on("add", lambda p: _handle_file_event(p, "add"))
    _watcher.on("unlink", lambda p: _handle_file_event(p, "unlink"))


def _handle_file_event(path: str, event: str) -> None:
    """Run the FileChanged hooks for a single ``(path, event)`` (fire-and-forget)."""
    log_for_debugging(f"FileChanged: {event} {path}")

    async def _run() -> None:
        from tabvis.utils.hooks import execute_file_changed_hooks  # noqa: PLC0415

        try:
            outcome = await execute_file_changed_hooks(path, event)
        except Exception as e:  # noqa: BLE001 - mirror the TS .catch
            msg = error_message(e)
            log_for_debugging(f"FileChanged hook failed: {msg}", {"level": "error"})
            if _notify_callback:
                _notify_callback(msg, True)
            return

        watch_paths = outcome.get("watchPaths") or []
        if len(watch_paths) > 0:
            update_watch_paths(watch_paths)
        for msg in outcome.get("systemMessages") or []:
            if _notify_callback:
                _notify_callback(msg, False)
        for r in outcome.get("results") or []:
            if not r.get("succeeded") and r.get("output"):
                if _notify_callback:
                    _notify_callback(r["output"], True)

    _spawn(_run())


def update_watch_paths(paths: list[str]) -> None:
    """Update the dynamic watch paths and restart watching if they changed."""
    global _dynamic_watch_paths, _dynamic_watch_paths_sorted
    if not _initialized:
        return
    sorted_paths = sorted(paths)
    if sorted_paths == _dynamic_watch_paths_sorted:
        return
    _dynamic_watch_paths = paths
    _dynamic_watch_paths_sorted = sorted_paths
    _restart_watching()


def _restart_watching() -> None:
    """Close the current watcher (if any) and re-resolve + restart watching."""
    global _watcher
    if _watcher:
        _maybe_await_close(_watcher.close())
        _watcher = None
    paths = _resolve_watch_paths()
    if len(paths) > 0:
        _start_watching(paths)


async def on_cwd_changed_for_hooks(old_cwd: str, new_cwd: str) -> None:
    """Run CwdChanged hooks on a cwd change."""
    global _current_cwd, _dynamic_watch_paths, _dynamic_watch_paths_sorted
    if old_cwd == new_cwd:
        return

    # Re-evaluate from the current snapshot so mid-session hook changes are picked up.
    config = _get_hooks_config_from_snapshot()
    current_has_env_hooks = (
        _count(config, "CwdChanged") > 0 or _count(config, "FileChanged") > 0
    )
    if not current_has_env_hooks:
        return
    _current_cwd = new_cwd

    from tabvis.utils.hooks import execute_cwd_changed_hooks  # noqa: PLC0415
    from tabvis.utils.session_environment import clear_cwd_env_files  # noqa: PLC0415

    await clear_cwd_env_files()
    try:
        hook_result = await execute_cwd_changed_hooks(old_cwd, new_cwd)
    except Exception as e:  # noqa: BLE001 - mirror the TS .catch
        msg = error_message(e)
        log_for_debugging(f"CwdChanged hook failed: {msg}", {"level": "error"})
        if _notify_callback:
            _notify_callback(msg, True)
        hook_result = {"results": [], "watchPaths": [], "systemMessages": []}

    _dynamic_watch_paths = hook_result.get("watchPaths") or []
    _dynamic_watch_paths_sorted = sorted(_dynamic_watch_paths)
    for msg in hook_result.get("systemMessages") or []:
        if _notify_callback:
            _notify_callback(msg, False)
    for r in hook_result.get("results") or []:
        if not r.get("succeeded") and r.get("output"):
            if _notify_callback:
                _notify_callback(r["output"], True)

    # Re-resolve matcher paths against the new cwd.
    if _initialized:
        _restart_watching()


def _dispose() -> None:
    """Tear down the watcher and reset module state."""
    global _watcher, _dynamic_watch_paths, _dynamic_watch_paths_sorted
    global _initialized, _has_env_hooks, _notify_callback
    if _watcher:
        _maybe_await_close(_watcher.close())
        _watcher = None
    _dynamic_watch_paths = []
    _dynamic_watch_paths_sorted = []
    _initialized = False
    _has_env_hooks = False
    _notify_callback = None


async def _async_dispose() -> None:
    """Async wrapper used by the cleanup registry (``registerCleanup(async () => dispose())``)."""
    _dispose()


def reset_file_changed_watcher_for_testing() -> None:
    """Reset all module state."""
    _dispose()


# ----------------------------------------------------------------------------------------------
# Async helpers
# ----------------------------------------------------------------------------------------------


def _spawn(coro: Any) -> None:
    """Fire-and-forget a coroutine (``void promise`` parity). Runs inline if no loop is active."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    loop.create_task(coro)


def _maybe_await_close(result: Any) -> None:
    """``void watcher.close()`` parity — fire-and-forget if the close returns an awaitable."""
    if asyncio.iscoroutine(result):
        _spawn(result)
