"""File locking

The TS file is a *lazy accessor*: it defers ``require('proper-lockfile')`` until the first
lock call, because that package pulls in ``graceful-fs`` (which monkey-patches every ``fs``
method on require, ~8ms) and a static import would tax cold startup paths like ``--help``.

``proper-lockfile`` is a Node-only npm package with no PyPI equivalent, so rather than add a
third-party dependency this module **reimplements the consumed slice in the stdlib**, matching
its directory-based locking algorithm (``lib/lockfile.js``):

- A lock is the **atomic creation of a sibling directory** ``<file>.lock`` (``os.mkdir`` is
  atomic / fails ``EEXIST`` if it already exists — the same primitive ``proper-lockfile`` uses).
- An existing lock is considered **stale** when its mtime is older than ``stale`` ms; stale
  locks are removed and re-acquired.
- ``lock`` retries acquisition (``retries`` count, exponential-ish backoff via ``min_timeout``),
  then keeps the lock **fresh** with a background updater thread that bumps the dir's mtime every
  ``update`` ms, so a long-held lock never appears stale to another process.
- The acquired lock is auto-released at process exit (``atexit``), mirroring the TS ``signal-exit``
  cleanup.

The consumed surface (from ``src/history.ts``) is ``lock(file, {stale, retries:{retries,
minTimeout}})`` → release coroutine. ``lock_sync`` / ``unlock`` / ``check`` are implemented for
parity with the TS exports.

Casing: Python identifiers are snake_case; ``LockOptions`` is a typed-dict of plain config
values (no wire round-trip). The npm export ``lockSync`` becomes ``lock_sync``.

Faithful-behavior notes:
- ``stale`` is floored at 2000ms and ``update`` clamped to ``[1000, stale/2]`` exactly as
  ``proper-lockfile`` does. ``stale <= 0`` disables stale-takeover (errors immediately on a held
  lock).
- A double-release is a no-op (TS: ``Lock is already released`` / ``ERELEASED`` — we swallow it
  to keep the release closure idempotent, which is how ``history.ts``'s ``finally`` uses it).
- ``check`` returns ``True`` when a non-stale lock dir exists, ``False`` when absent or stale.
- ``lock``/``check`` resolve ``file`` to an absolute (canonical) path first (TS ``realpath:true``
  default); we use ``os.path.realpath`` to match symlink resolution.
- The async ``lock`` runs the (blocking) acquisition in a thread executor so it does not block the
  event loop, then returns an **async** release callable (TS ``Promise<() => Promise<void>>``).

``proper-lockfile``'s ``onCompromised`` callback and mtime-precision probing are not
modeled — the updater thread simply refreshes the mtime and best-effort ignores races. This is
sufficient for the single-writer ``history.jsonl`` append the spine uses; compromise detection is
not supported.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypedDict


class RetryOptions(TypedDict, total=False):
    retries: int
    minTimeout: int
    maxTimeout: int


class LockOptions(TypedDict, total=False):
    stale: int
    update: int
    retries: int | RetryOptions
    realpath: bool
    lockfilePath: str


class UnlockOptions(TypedDict, total=False):
    realpath: bool
    lockfilePath: str


class CheckOptions(TypedDict, total=False):
    stale: int
    realpath: bool
    lockfilePath: str


class LockError(Exception):
    """Raised when a lock cannot be acquired or released (carries a ``code``)."""

    def __init__(self, message: str, code: str, file: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.file = file


class _LockState:
    def __init__(self, lockfile_path: str, stale_ms: int, update_ms: int) -> None:
        self.lockfile_path = lockfile_path
        self.stale_ms = stale_ms
        self.update_ms = update_ms
        self.released = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start_updater(self) -> None:
        def _run() -> None:
            while not self._stop.wait(self.update_ms / 1000.0):
                if self.released:
                    return
                # Keep the lock fresh: bump mtime so other processes don't see it as stale.
                try:
                    now = time.time()
                    os.utime(self.lockfile_path, (now, now))
                except OSError:
                    # Lock dir vanished or unreachable — stop refreshing.
                    return

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop_updater(self) -> None:
        self._stop.set()


# Active locks: canonical file path -> _LockState. Process-global (matches the TS ``locks`` map).
_locks: dict[str, _LockState] = {}
_locks_guard = threading.Lock()


def _get_lock_file(file: str, options: LockOptions | UnlockOptions | CheckOptions | None) -> str:
    if options and options.get("lockfilePath"):
        return str(options["lockfilePath"])
    return f"{file}.lock"


def _resolve_canonical(file: str, options: LockOptions | UnlockOptions | CheckOptions | None) -> str:
    # TS default realpath:true → resolve symlinks; else just absolutize.
    if options is not None and options.get("realpath") is False:
        return os.path.abspath(file)
    return os.path.realpath(file)


def _is_lock_stale(lockfile_path: str, stale_ms: int) -> bool:
    try:
        mtime_ms = os.stat(lockfile_path).st_mtime * 1000.0
    except OSError:
        return True
    return mtime_ms < (time.time() * 1000.0) - stale_ms


def _normalize_retries(retries: int | RetryOptions | None) -> RetryOptions:
    if retries is None:
        return {"retries": 0}
    if isinstance(retries, int):
        return {"retries": retries}
    return retries


def _acquire_lock_blocking(file: str, options: LockOptions) -> _LockState:
    """Acquire the lock synchronously (blocking, with retry/backoff). Returns the lock state."""
    canonical = _resolve_canonical(file, options)
    lockfile_path = _get_lock_file(canonical, options)

    # Option normalization, mirroring proper-lockfile's `lock()` defaults.
    stale_ms = max(int(options.get("stale", 10000) or 0), 2000)
    raw_update = options.get("update")
    if raw_update is None:
        update_ms = stale_ms // 2
    else:
        update_ms = int(raw_update) or 0
    update_ms = max(min(update_ms, stale_ms // 2), 1000)

    retry_opts = _normalize_retries(options.get("retries"))
    max_retries = int(retry_opts.get("retries", 0) or 0)
    min_timeout = int(retry_opts.get("minTimeout", 1000) or 0)

    attempt = 0
    while True:
        try:
            _try_mkdir_lock(canonical, lockfile_path, stale_ms)
            state = _LockState(lockfile_path, stale_ms, update_ms)
            with _locks_guard:
                _locks[canonical] = state
            state.start_updater()
            return state
        except LockError:
            if attempt >= max_retries:
                raise
            # Exponential-ish backoff (retry lib: minTimeout * 2**attempt).
            time.sleep((min_timeout * (2**attempt)) / 1000.0)
            attempt += 1


def _try_mkdir_lock(canonical: str, lockfile_path: str, stale_ms: int) -> None:
    """One acquisition attempt via atomic ``mkdir``; raises ``LockError(ELOCKED)`` if held."""
    try:
        os.mkdir(lockfile_path)
        return  # Acquired.
    except FileExistsError:
        pass

    # Lock dir already exists — check staleness.
    if stale_ms <= 0:
        raise LockError("Lock file is already being held", "ELOCKED", canonical)

    if not _is_lock_stale(lockfile_path, stale_ms):
        raise LockError("Lock file is already being held", "ELOCKED", canonical)

    # Stale: remove and retry once with stale-check disabled (matches TS recursion guard).
    try:
        os.rmdir(lockfile_path)
    except OSError:
        pass
    try:
        os.mkdir(lockfile_path)
    except FileExistsError as err:
        raise LockError("Lock file is already being held", "ELOCKED", canonical) from err


def _release(canonical: str, options: UnlockOptions | None) -> None:
    with _locks_guard:
        state = _locks.get(canonical)
        if state is None:
            return
        del _locks[canonical]
    state.released = True
    state.stop_updater()
    try:
        os.rmdir(state.lockfile_path)
    except OSError:
        pass


def _unlock_blocking(file: str, options: UnlockOptions | None) -> None:
    canonical = _resolve_canonical(file, options)
    with _locks_guard:
        state = _locks.get(canonical)
    if state is None:
        raise LockError("Lock is not acquired/owned by you", "ENOTACQUIRED", canonical)
    _release(canonical, options)


def _check_blocking(file: str, options: CheckOptions | None) -> bool:
    canonical = _resolve_canonical(file, options)
    lockfile_path = _get_lock_file(canonical, options)
    stale_ms = max(int((options or {}).get("stale", 10000) or 0), 2000)
    if not os.path.exists(lockfile_path):
        return False
    return not _is_lock_stale(lockfile_path, stale_ms)


# --- public API (mirrors proper-lockfile's promise/sync exports) ------------------------


async def lock(
    file: str, options: LockOptions | None = None
) -> Callable[[], Awaitable[None]]:
    """Acquire a lock on ``file``; return an (async) release callable.

    Runs the blocking acquisition in a thread executor so the event loop is not blocked. The
    returned ``release`` is idempotent (double-release is a no-op).
    """
    opts = options or {}
    loop = asyncio.get_event_loop()
    state = await loop.run_in_executor(None, _acquire_lock_blocking, file, opts)
    canonical = _resolve_canonical(file, opts)

    async def release() -> None:
        if state.released:
            return
        await asyncio.get_event_loop().run_in_executor(None, _release, canonical, {"realpath": False})

    return release


def lock_sync(file: str, options: LockOptions | None = None) -> Callable[[], None]:
    """Synchronous lock acquisition; return a synchronous release callable."""
    opts = options or {}
    state = _acquire_lock_blocking(file, opts)
    canonical = _resolve_canonical(file, opts)

    def release() -> None:
        if state.released:
            return
        _release(canonical, {"realpath": False})

    return release


async def unlock(file: str, options: UnlockOptions | None = None) -> None:
    """Release a previously acquired lock (raises ``ENOTACQUIRED`` if not held)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _unlock_blocking, file, options)


async def check(file: str, options: CheckOptions | None = None) -> bool:
    """Whether ``file`` currently has a live (non-stale) lock."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _check_blocking, file, options)


@atexit.register
def _cleanup_locks_on_exit() -> None:  # pragma: no cover - process-exit hook
    with _locks_guard:
        states = list(_locks.values())
        _locks.clear()
    for state in states:
        state.released = True
        state.stop_updater()
        try:
            os.rmdir(state.lockfile_path)
        except OSError:
            pass
