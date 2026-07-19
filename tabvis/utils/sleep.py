"""Abort-responsive sleep + promise-timeout race

The TS module exports two helpers built on the web/Node ``AbortSignal`` + ``setTimeout``:

- ``sleep(ms, signal?, opts?)`` — resolves after ``ms`` milliseconds, or immediately when
  ``signal`` aborts (so backoff loops don't block shutdown). By default abort resolves
  silently and the caller checks ``signal.aborted`` afterwards; ``throwOnAbort`` /
  ``abortError`` make abort *reject* instead.
- ``withTimeout(promise, ms, message)`` — races an awaitable against a timeout, raising
  ``Error(message)`` if it doesn't settle in time. It does NOT cancel the underlying work.

Casing: Python identifiers are snake_case; ``throwOnAbort``/``abortError``/``unref`` become
``throw_on_abort``/``abort_error``/``unref``. The ``signal`` arg accepts the asyncio-based
:class:`tabvis.utils.abort.AbortSignal` shim (reused, not reinvented) — any object exposing an
``aborted`` property and an awaitable ``wait()`` works.

Faithful-behavior notes:
- TS times in *milliseconds*; we keep the ms argument and divide by 1000 for ``asyncio.sleep``.
- TS ``unref()`` keeps a pending timer from blocking process exit. asyncio tasks don't pin the
  loop the way libuv timers do, so ``unref`` is accepted-and-ignored (documented no-op).
- TS checks ``signal?.aborted`` BEFORE arming the timer and returns synchronously; we mirror
  that ordering so an already-aborted signal never waits a tick.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def sleep(
    ms: float,
    signal: Any | None = None,
    *,
    throw_on_abort: bool = False,
    abort_error: Callable[[], BaseException] | None = None,
    unref: bool = False,
) -> None:
    """Sleep for ``ms`` milliseconds, returning early (or raising) when ``signal`` aborts.

    - ``signal``: optional abort signal (e.g. :class:`tabvis.utils.abort.AbortSignal`). Must
      expose an ``aborted`` bool and an awaitable ``wait()`` that resolves on abort.
    - ``throw_on_abort`` / ``abort_error``: when either is set, an abort *raises* instead of
      returning silently. ``abort_error`` (a factory) takes precedence as the raised error;
      otherwise ``Error('aborted')`` (here :class:`RuntimeError`) is raised.
    """
    # Check aborted state BEFORE arming the timer (mirrors the TS dead-zone guard).
    if signal is not None and getattr(signal, "aborted", False):
        if throw_on_abort or abort_error is not None:
            raise abort_error() if abort_error is not None else RuntimeError("aborted")
        return

    # No signal: a plain sleep.
    if signal is None:
        await asyncio.sleep(ms / 1000)
        return

    # Race the timer against the abort signal. Whichever finishes first wins.
    sleep_task = asyncio.ensure_future(asyncio.sleep(ms / 1000))
    abort_task = asyncio.ensure_future(_as_awaitable(signal.wait()))
    try:
        done, _pending = await asyncio.wait(
            {sleep_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (sleep_task, abort_task):
            if not task.done():
                task.cancel()

    if sleep_task in done and not sleep_task.cancelled():
        # Timer fired first — normal completion.
        return

    # Aborted first.
    if throw_on_abort or abort_error is not None:
        raise abort_error() if abort_error is not None else RuntimeError("aborted")
    return


async def _as_awaitable(value: Awaitable[Any]) -> Any:
    """Normalize ``signal.wait()`` (a coroutine or awaitable) into something ``wait`` accepts."""
    return await value


async def with_timeout(promise: Awaitable[T], ms: float, message: str) -> T:
    """Race ``promise`` against a ``ms``-millisecond timeout.

    Raises :class:`TimeoutError` carrying ``message`` if the awaitable doesn't settle in time.
    Does NOT cancel the underlying work beyond cancelling the awaiting task — if ``promise`` is
    backed by a runaway operation, that keeps running; this just returns control to the caller.
    """
    main_task = asyncio.ensure_future(_as_awaitable(promise))
    try:
        return await asyncio.wait_for(main_task, timeout=ms / 1000)
    except TimeoutError as exc:
        # asyncio.wait_for raises asyncio.TimeoutError, which is an alias of the builtin
        # TimeoutError in Python 3.11+. Re-raise carrying the caller's message (parity with
        # the TS `Error(message)`).
        raise TimeoutError(message) from exc
