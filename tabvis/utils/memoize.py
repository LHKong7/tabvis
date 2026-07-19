"""Memoization helpers

Three constructors, implemented with their real semantics (NOT a bare ``functools.lru_cache``):

* :func:`memoize_with_ttl` — sync write-through cache. Fresh entry → return immediately;
  stale entry → return the *stale* value but schedule a background refresh; cold miss →
  block and compute. The background refresh is identity-guarded so a concurrent
  ``cache.clear()`` + cold-miss (which stores a newer entry) is not clobbered by the
  stale refresh's result.
* :func:`memoize_with_ttl_async` — async variant with the same write-through semantics plus
  in-flight cold-miss dedup (concurrent cold-miss callers share one ``f()`` invocation).
* :func:`memoize_with_lru` — bounded LRU cache (least-recently-used eviction) keyed by a
  user-supplied ``cache_fn``. Replaces the npm ``lru-cache`` with a stdlib ``OrderedDict``.

The TS ``memoized.cache`` object (``clear``/``size``/``delete``/``get``/``has``) is exposed as
attributes on the returned callable so call sites read identically (e.g. ``memoized.cache.clear()``).

Casing: Python identifiers are snake_case; there are no wire-key dicts here (purely runtime
caches), so nothing round-trips to JSON/the transcript.

Async refresh model: the TS ``Promise.resolve().then(() => f(...))`` schedules a microtask on
the event loop. In :func:`memoize_with_ttl` (sync) the equivalent is ``asyncio.get_running_loop()
.call_soon`` when a loop is running; with no running loop (a plain sync call site) the refresh runs
inline after returning the stale value would not be possible, so it is skipped and the stale value is
returned — the entry stays marked refreshing only while a loop is available to clear it. This matches
the headless reality: the TS sync variant also only refreshes opportunistically on the next tick.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeVar

from tabvis.utils.log import log_error
from tabvis.utils.slow_operations import json_stringify

_Result = TypeVar("_Result")

# Default cache lifetime: 5 minutes (matches the TS ``5 * 60 * 1000`` default).
DEFAULT_CACHE_LIFETIME_MS = 5 * 60 * 1000


def _now_ms() -> int:
    """Wall-clock milliseconds (parity with JS ``Date.now()``)."""
    import time

    return int(time.time() * 1000)


class _CacheEntry(Generic[_Result]):
    """A cached value plus its timestamp and a refresh flag (TS ``CacheEntry<T>``)."""

    __slots__ = ("value", "timestamp", "refreshing")

    def __init__(self, value: _Result, timestamp: int, refreshing: bool = False) -> None:
        self.value = value
        self.timestamp = timestamp
        self.refreshing = refreshing


class _TTLCacheHandle:
    """The ``memoized.cache`` object for the sync/async TTL variants — only ``clear``."""

    def __init__(self, clear: Callable[[], None]) -> None:
        self.clear = clear


class _LRUCacheHandle:
    """The ``memoized.cache`` object for the LRU variant (clear/size/delete/get/has)."""

    def __init__(
        self,
        *,
        clear: Callable[[], None],
        size: Callable[[], int],
        delete: Callable[[str], bool],
        get: Callable[[str], Any],
        has: Callable[[str], bool],
    ) -> None:
        self.clear = clear
        self.size = size
        self.delete = delete
        self.get = get
        self.has = has


def memoize_with_ttl(
    f: Callable[..., _Result],
    cache_lifetime_ms: int = DEFAULT_CACHE_LIFETIME_MS,
) -> Callable[..., _Result]:
    """Memoize ``f`` with a write-through TTL cache.

    - cache fresh → return immediately
    - cache stale → return the stale value, refresh in the background
    - no cache → block and compute

    The returned callable carries a ``.cache`` handle with a ``clear()`` method.
    """
    cache: dict[str, _CacheEntry[_Result]] = {}

    def _schedule_refresh(key: str, args: tuple, kwargs: dict, cached: _CacheEntry[_Result]) -> None:
        def _do_refresh() -> None:
            try:
                new_value = f(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - mirror the TS .catch branch
                log_error(e)
                # Identity-guard: only delete if still the same stale entry.
                if cache.get(key) is cached:
                    cache.pop(key, None)
                return
            # Identity-guard: a concurrent clear() + cold-miss stores a newer entry;
            # don't clobber it with the stale refresh's result.
            if cache.get(key) is cached:
                cache[key] = _CacheEntry(new_value, _now_ms(), refreshing=False)

        # Mirror Promise.resolve().then(...) — schedule on the running loop's next tick.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.call_soon(_do_refresh)
        else:
            # No event loop: run the refresh synchronously after we've decided to
            # return the stale value (the value to return is already captured below).
            _do_refresh()

    def memoized(*args: Any, **kwargs: Any) -> _Result:
        key = json_stringify(list(args))
        cached = cache.get(key)
        now = _now_ms()

        # Populate cache (cold miss).
        if cached is None:
            value = f(*args, **kwargs)
            cache[key] = _CacheEntry(value, now, refreshing=False)
            return value

        # Stale and not already refreshing → return stale, refresh in background.
        if now - cached.timestamp > cache_lifetime_ms and not cached.refreshing:
            cached.refreshing = True
            stale_value = cached.value
            _schedule_refresh(key, args, kwargs, cached)
            return stale_value

        return cache[key].value

    memoized.cache = _TTLCacheHandle(clear=cache.clear)  # type: ignore[attr-defined]
    return memoized


def memoize_with_ttl_async(
    f: Callable[..., Awaitable[_Result]],
    cache_lifetime_ms: int = DEFAULT_CACHE_LIFETIME_MS,
) -> Callable[..., Awaitable[_Result]]:
    """Async write-through TTL cache.

    Same semantics as :func:`memoize_with_ttl` plus in-flight cold-miss dedup: concurrent
    cold-miss callers share a single ``f()`` invocation. ``cache.clear()`` also clears the
    in-flight map so a stale in-flight promise is not handed to the next caller.
    """
    cache: dict[str, _CacheEntry[_Result]] = {}
    in_flight: dict[str, asyncio.Future[_Result]] = {}

    def _clear() -> None:
        cache.clear()
        in_flight.clear()

    async def memoized(*args: Any, **kwargs: Any) -> _Result:
        key = json_stringify(list(args))
        cached = cache.get(key)
        now = _now_ms()

        # Cold miss — block, with in-flight dedup.
        if cached is None:
            pending = in_flight.get(key)
            if pending is not None:
                return await pending
            task: asyncio.Future[_Result] = asyncio.ensure_future(f(*args, **kwargs))
            in_flight[key] = task
            try:
                result = await task
                # Identity-guard: clear() during the await should discard this result.
                if in_flight.get(key) is task:
                    cache[key] = _CacheEntry(result, now, refreshing=False)
                return result
            finally:
                if in_flight.get(key) is task:
                    in_flight.pop(key, None)

        # Stale and not already refreshing → return stale, refresh in background.
        if now - cached.timestamp > cache_lifetime_ms and not cached.refreshing:
            cached.refreshing = True
            stale_entry = cached

            async def _refresh() -> None:
                try:
                    new_value = await f(*args, **kwargs)
                except Exception as e:  # noqa: BLE001 - mirror the TS .catch branch
                    log_error(e)
                    if cache.get(key) is stale_entry:
                        cache.pop(key, None)
                    return
                if cache.get(key) is stale_entry:
                    cache[key] = _CacheEntry(new_value, _now_ms(), refreshing=False)

            # Fire-and-forget background refresh (TS f(...).then(...).catch(...)).
            asyncio.ensure_future(_refresh())
            return cached.value

        return cache[key].value

    memoized.cache = _TTLCacheHandle(clear=_clear)  # type: ignore[attr-defined]
    return memoized


# Default LRU cache size (TS ``maxCacheSize = 100``).
DEFAULT_MAX_CACHE_SIZE = 100


def memoize_with_lru(
    f: Callable[..., _Result],
    cache_fn: Callable[..., str],
    max_cache_size: int = DEFAULT_MAX_CACHE_SIZE,
) -> Callable[..., _Result]:
    """Memoize ``f`` with an LRU cache.

    ``cache_fn(*args)`` computes the cache key. The least-recently-*used* entry is evicted
    when the cache exceeds ``max_cache_size``. ``get`` *peeks* (does not promote recency), to
    match the TS ``cache.peek(key)``.

    The returned callable carries a ``.cache`` handle (clear/size/delete/get/has).
    """
    cache: OrderedDict[str, _Result] = OrderedDict()

    def memoized(*args: Any, **kwargs: Any) -> _Result:
        key = cache_fn(*args, **kwargs)
        if key in cache:
            cache.move_to_end(key)  # mark most-recently-used (lru-cache get() promotes)
            return cache[key]

        result = f(*args, **kwargs)
        cache[key] = result
        cache.move_to_end(key)
        # Evict the least-recently-used entries past the cap.
        while len(cache) > max_cache_size:
            cache.popitem(last=False)
        return result

    def _delete(key: str) -> bool:
        if key in cache:
            del cache[key]
            return True
        return False

    def _peek(key: str) -> Any:
        # peek() does not update recency — observe only, don't promote.
        return cache.get(key)

    memoized.cache = _LRUCacheHandle(  # type: ignore[attr-defined]
        clear=cache.clear,
        size=lambda: len(cache),
        delete=_delete,
        get=_peek,
        has=lambda key: key in cache,
    )
    return memoized
