"""Async-generator combinators

Helpers for consuming/merging async generators:

- ``last_x(gen)`` — the last yielded value (raises if the generator yields nothing).
- ``return_value(gen)`` — drain a generator and return its *return value*. Python async
  generators cannot ``return`` a value to ``async for`` (PEP 525 forbids non-None returns), so
  the TS ``AsyncGenerator<unknown, A>`` shape is modelled with a sentinel-tagged final yield (see
  faithful-behavior note).
- ``all(generators, concurrency_cap)`` — run generators concurrently up to a cap, yielding
  values as they arrive (``undefined``/``None`` values are skipped, matching the TS guard).
- ``to_array(gen)`` — collect all yields into a list.
- ``from_array(values)`` — yield each value of a list.

Casing: Python identifiers are snake_case (``lastX``→``last_x``, ``returnValue``→``return_value``,
``toArray``→``to_array``, ``fromArray``→``from_array``, ``concurrencyCap``→``concurrency_cap``).
The plain function ``all`` keeps its name (it does not shadow the builtin at call sites that
import it explicitly).

Faithful-behavior notes:
- TS ``Infinity`` default for the concurrency cap → Python ``None`` meaning "unbounded".
- The TS ``all`` skips ``value !== undefined`` before yielding; we skip ``value is None`` to
  match (the TS generators here yield non-None payloads).
- ``return_value``: the TS contract relies on an async generator's *return value*. Since Python
  forbids that, ``return_value`` instead expects the generator to signal its result by yielding a
  ``ReturnSentinel(value)`` as its final item (or returns ``None`` if absent). Callers that need
  this should wrap their result accordingly. This is a permanent, documented divergence from the
  TS return-value semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

A = TypeVar("A")
T = TypeVar("T")

# Sentinel marking "no value seen" (parity with the TS ``Symbol('NO_VALUE')``).
_NO_VALUE = object()


async def last_x(gen: AsyncIterator[A]) -> A:
    """Return the last value yielded by ``gen``; raise if it yields nothing."""
    last_value: Any = _NO_VALUE
    async for item in gen:
        last_value = item
    if last_value is _NO_VALUE:
        raise RuntimeError("No items in generator")
    return last_value


@dataclass
class ReturnSentinel(Generic[A]):
    """Wrapper a generator yields as its final item to convey a TS-style return value."""

    value: A


async def return_value(gen: AsyncIterator[Any]) -> Any:
    """Drain ``gen`` and return its result.

    See the module docstring: Python async generators can't return a value to ``async for``,
    so the result is conveyed via a trailing :class:`ReturnSentinel`. Returns ``None`` when no
    sentinel is yielded.
    """
    result: Any = None
    async for item in gen:
        if isinstance(item, ReturnSentinel):
            result = item.value
    return result


async def all(  # noqa: A001 - faithful name from src/utils/generators.ts `all`
    generators: Sequence[AsyncIterator[A]],
    concurrency_cap: int | None = None,
) -> AsyncGenerator[A, None]:
    """Run ``generators`` concurrently (up to ``concurrency_cap``), yielding values as they arrive.

    ``None`` yields are skipped (mirrors the TS ``value !== undefined`` guard). When a generator
    finishes, the next waiting generator starts so at most ``concurrency_cap`` run at once
    (``None`` = unbounded).
    """
    waiting: list[AsyncIterator[A]] = list(generators)
    # Map each in-flight task -> the generator it pulls from.
    pending: dict[asyncio.Task[Any], AsyncIterator[A]] = {}

    def schedule(generator: AsyncIterator[A]) -> None:
        task = asyncio.ensure_future(_anext_or_done(generator))
        pending[task] = generator

    cap = concurrency_cap if concurrency_cap is not None else len(waiting)
    # Start the initial batch up to the concurrency cap.
    while len(pending) < cap and waiting:
        schedule(waiting.pop(0))

    while pending:
        done, _ = await asyncio.wait(pending.keys(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            generator = pending.pop(task)
            is_done, value = task.result()
            if not is_done:
                schedule(generator)
                if value is not None:
                    yield value
            elif waiting:
                # One generator finished — start a new one.
                schedule(waiting.pop(0))


async def _anext_or_done(generator: AsyncIterator[A]) -> tuple[bool, Any]:
    """Pull one item; return ``(done, value)`` mirroring the JS ``IteratorResult`` shape."""
    try:
        value = await generator.__anext__()
        return (False, value)
    except StopAsyncIteration:
        return (True, None)


async def to_array(gen: AsyncIterator[A]) -> list[A]:
    """Collect every value yielded by ``gen`` into a list."""
    result: list[A] = []
    async for item in gen:
        result.append(item)
    return result


async def from_array(values: Sequence[T]) -> AsyncGenerator[T, None]:
    """Yield each value of ``values`` in order."""
    for value in values:
        yield value
