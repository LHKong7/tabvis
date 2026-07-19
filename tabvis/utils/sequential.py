"""``sequential``

Wraps an async function so that concurrent calls are executed **one at a time, in arrival
order**, while each caller still awaits and receives its own return value (or exception).
Useful for operations that must not interleave — file writes, database updates — that would
otherwise race.

Casing: Python identifiers are snake_case. No wire-key dicts are involved.

Faithful-behavior notes:
- The TS implementation pushes ``{args, resolve, reject, context}`` onto a queue and drains it
  in a single ``processQueue`` pass (guarded by a ``processing`` flag), awaiting ``fn`` for each
  item before moving on, and re-kicking the drain if items arrived mid-drain. The net guarantee
  is: **strict FIFO, never two invocations of ``fn`` in flight at once**.
- This implementation provides that guarantee with an :class:`asyncio.Lock`. Each wrapped call awaits
  the lock; because asyncio acquires a lock's waiters in FIFO order, callers run in the order
  they entered ``await wrapped(...)``, one at a time. A caller's exception propagates to that
  caller only (the TS ``reject``), and the lock is always released (``finally``), so a failing
  call never wedges the queue — matching the TS ``try/catch`` that resolves/rejects each item
  independently and continues draining.
- The TS preserves ``this`` via ``fn.apply(context, args)``. Python has no implicit ``this``;
  callers bind methods themselves, so the wrapped function is simply invoked as
  ``fn(*args, **kwargs)``. ``**kwargs`` is accepted as a convenience (TS only had positional
  ``args``) and forwarded verbatim.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

R = TypeVar("R")


def sequential(
    fn: Callable[..., Awaitable[R]],
) -> Callable[..., Awaitable[R]]:
    """Return a wrapper that serializes concurrent calls to ``fn`` (FIFO, one at a time)."""
    lock = asyncio.Lock()

    async def wrapped(*args: Any, **kwargs: Any) -> R:
        async with lock:
            return await fn(*args, **kwargs)

    return wrapped
