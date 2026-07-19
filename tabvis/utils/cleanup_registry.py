"""Graceful-shutdown cleanup registry

A process-global set of cleanup callbacks that run during graceful shutdown. The TS file is
deliberately split from ``gracefulShutdown.ts`` to avoid a circular import; this implementation keeps
the same minimal surface: ``register_cleanup`` (add + return an unregister closure) and
``run_cleanup_functions`` (await them all).

Casing: Python identifiers are snake_case. No wire-key dicts are involved.

Faithful-behavior notes:
- The registry is a module-level ``set`` (TS ``new Set<() => Promise<void>>()``). Membership
  is by callable identity, so the unregister closure removes exactly the one that was added.
- ``register_cleanup`` returns an idempotent unregister: the TS ``cleanupFunctions.delete(fn)``
  is a no-op once already removed, and ``set.discard`` matches that (never raises).
- ``run_cleanup_functions`` mirrors ``Promise.all(Array.from(...).map(fn => fn()))`` — it
  snapshots the current members, invokes each, and awaits them **concurrently** (not serially).
  A snapshot is taken first so a callback that unregisters itself mid-run does not mutate the
  set being iterated.
- TS allows sync-or-async cleanups (``() => Promise<void>`` but JS happily awaits a non-promise
  return). The implementation accepts callbacks returning either ``None`` or an awaitable; sync returns
  are simply not awaited, matching that flexibility.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

CleanupFn = Callable[[], Awaitable[None] | None]

_cleanup_functions: set[CleanupFn] = set()


def register_cleanup(cleanup_fn: CleanupFn) -> Callable[[], None]:
    """Register a shutdown cleanup; return a closure that unregisters it."""
    _cleanup_functions.add(cleanup_fn)

    def unregister() -> None:
        _cleanup_functions.discard(cleanup_fn)

    return unregister


async def run_cleanup_functions() -> None:
    """Run every registered cleanup concurrently and await completion."""
    awaitables: list[Awaitable[None]] = []
    for fn in list(_cleanup_functions):
        result = fn()
        if inspect.isawaitable(result):
            awaitables.append(result)
    if awaitables:
        await asyncio.gather(*awaitables)
