"""File state cache

The TS module wraps ``lru-cache`` (``LRUCache<string, FileState>``) in a
:class:`FileStateCache` that normalizes every path key before access, so callers
get consistent hits regardless of relative/absolute paths, redundant ``..``
segments, or mixed separators. It supports both count-based (``max`` entries) and
byte-size-based (``maxSize`` bytes) eviction, where an entry's size is
``max(1, byteLength(content))``.

``FileState`` is a plain ``dict`` (it round-trips into the transcript via
``ToolUseContext.read_file_state``), so it keeps its wire keys — including the
camelCase ``isPartialView``. Python identifiers in this module are snake_case.

Iteration order matches ``lru-cache``: most-recently-used first. ``get``/``set``
refresh recency (most-recently-used). Eviction drops the least-recently-used.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from collections.abc import Iterator
from typing import Any, TypedDict


class FileState(TypedDict, total=False):
    """A cached view of a file the model has read.

    Wire keys (camelCase ``isPartialView`` kept — this dict round-trips into the
    transcript). ``content`` holds what was cached for the path; for partial
    auto-injected views it holds the RAW disk bytes (see TS docstring).
    """

    content: str  # required in practice
    timestamp: float  # required in practice (ms epoch)
    offset: int | None
    limit: int | None
    # True when the entry came from auto-injection (e.g. TABVIS.md) and the
    # injected content did not match disk; Edit/Write must require an explicit
    # Read first. ``content`` then holds RAW disk bytes (for diffing).
    isPartialView: bool


# Default max entries for read file state caches.
READ_FILE_STATE_CACHE_SIZE = 100

# Default size limit for file state caches (25MB). Prevents unbounded memory
# growth from large file contents.
DEFAULT_MAX_CACHE_SIZE_BYTES = 25 * 1024 * 1024


def _normalize(key: str) -> str:
    """Normalize a file path for use as a cache key.

    Node's ``normalize`` resolves ``.``/``..`` segments and collapses redundant
    separators *without* touching the filesystem (no symlink/abs resolution).
    ``os.path.normpath`` is the closest stdlib equivalent. It additionally strips
    a trailing separator, which is harmless for cache identity.

    Note: Node's normalize preserves the platform separator; on POSIX both use
    ``/``. ``normpath`` also collapses ``//`` -> ``/`` (Node keeps a leading
    ``//`` on POSIX, but that edge case does not arise for real file paths here).
    Node's ``normalize('')`` -> ``'.'`` and ``normpath('')`` -> ``'.'`` agree.
    """
    return os.path.normpath(key)


def _entry_size(value: FileState) -> int:
    """Size of an entry in bytes: ``max(1, byteLength(content))`` (UTF-8)."""
    content = value.get("content", "") if isinstance(value, dict) else ""
    if not isinstance(content, str):
        content = str(content)
    return max(1, len(content.encode("utf-8")))


class FileStateCache:
    """LRU cache mapping normalized path -> :class:`FileState`.

    Mirrors the ``lru-cache``-backed TS class: count cap (``max``), byte-size cap
    (``maxSize`` via per-entry ``content`` byte length), recency refresh on
    ``get``/``set``, and LRU eviction. Iteration yields most-recently-used first.
    """

    def __init__(self, max_entries: int, max_size_bytes: int) -> None:
        # OrderedDict ordered LRU -> MRU (end == most recently used), to match
        # lru-cache's move-to-end-on-touch semantics. Public iteration reverses
        # this to MRU-first.
        self._cache: OrderedDict[str, FileState] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._max = max_entries
        self._max_size = max_size_bytes
        self._calculated_size = 0

    # --- core access (path-normalizing) ---
    def get(self, key: str) -> FileState | None:
        nkey = _normalize(key)
        if nkey not in self._cache:
            return None
        # Touch: most recently used.
        self._cache.move_to_end(nkey)
        return self._cache[nkey]

    def set(self, key: str, value: FileState) -> FileStateCache:
        nkey = _normalize(key)
        new_size = _entry_size(value)
        if nkey in self._cache:
            # Replace: adjust running size, then re-touch.
            self._calculated_size -= self._sizes.get(nkey, 0)
        self._cache[nkey] = value
        self._cache.move_to_end(nkey)
        self._sizes[nkey] = new_size
        self._calculated_size += new_size
        self._evict()
        return self

    def has(self, key: str) -> bool:
        return _normalize(key) in self._cache

    def delete(self, key: str) -> bool:
        nkey = _normalize(key)
        if nkey not in self._cache:
            return False
        self._calculated_size -= self._sizes.pop(nkey, 0)
        del self._cache[nkey]
        return True

    def clear(self) -> None:
        self._cache.clear()
        self._sizes.clear()
        self._calculated_size = 0

    # --- eviction ---
    def _evict(self) -> None:
        # Count cap.
        while len(self._cache) > self._max:
            self._pop_lru()
        # Byte-size cap. lru-cache evicts until total <= maxSize; a single entry
        # larger than maxSize is still kept (it is the only one and removing it
        # would not be useful) — but lru-cache will leave it as the sole entry.
        while self._max_size > 0 and self._calculated_size > self._max_size and len(self._cache) > 1:
            self._pop_lru()

    def _pop_lru(self) -> None:
        # OrderedDict head == least recently used.
        lru_key, _ = self._cache.popitem(last=False)
        self._calculated_size -= self._sizes.pop(lru_key, 0)

    # --- introspection (parity getters) ---
    @property
    def size(self) -> int:
        return len(self._cache)

    @property
    def max(self) -> int:
        return self._max

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def calculated_size(self) -> int:
        return self._calculated_size

    # --- iteration (most-recently-used first, matching lru-cache) ---
    def keys(self) -> Iterator[str]:
        return reversed(self._cache)

    def entries(self) -> Iterator[tuple[str, FileState]]:
        for k in reversed(self._cache):
            yield k, self._cache[k]

    def dump(self) -> list[tuple[str, dict[str, Any]]]:
        """Serialize entries (LRU -> MRU order) for ``load`` round-tripping.

        lru-cache's ``dump`` emits ``[key, {value, ...meta}]`` LRU-first; we keep
        the same LRU-first order and a minimal ``{value}`` payload, which is all
        :meth:`load` consumes here.
        """
        return [(k, {"value": self._cache[k]}) for k in self._cache]

    def load(self, entries: list[tuple[str, dict[str, Any]]]) -> None:
        """Repopulate from a :meth:`dump` payload (LRU -> MRU order)."""
        self.clear()
        for key, meta in entries:
            self.set(key, meta["value"])


def create_file_state_cache_with_size_limit(
    max_entries: int,
    max_size_bytes: int = DEFAULT_MAX_CACHE_SIZE_BYTES,
) -> FileStateCache:
    """Factory for a size-limited :class:`FileStateCache`."""
    return FileStateCache(max_entries, max_size_bytes)


def cache_to_object(cache: FileStateCache) -> dict[str, FileState]:
    """Convert a cache to a plain dict (used by compaction)."""
    return dict(cache.entries())


def cache_keys(cache: FileStateCache) -> list[str]:
    """All keys in the cache (most-recently-used first)."""
    return list(cache.keys())


def clone_file_state_cache(cache: FileStateCache) -> FileStateCache:
    """Clone a cache, preserving its size-limit configuration."""
    cloned = create_file_state_cache_with_size_limit(cache.max, cache.max_size)
    cloned.load(cache.dump())
    return cloned


def merge_file_state_caches(
    first: FileStateCache,
    second: FileStateCache,
) -> FileStateCache:
    """Merge two caches; entries from ``second`` win only when more recent.

    More recent is decided by ``timestamp``. Entries unique to ``first`` are kept;
    entries unique to ``second`` are added.
    """
    merged = clone_file_state_cache(first)
    for file_path, file_state in second.entries():
        existing = merged.get(file_path)
        if existing is None or file_state.get("timestamp", 0) > existing.get("timestamp", 0):
            merged.set(file_path, file_state)
    return merged
