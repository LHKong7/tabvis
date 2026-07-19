"""In-process settings-write tracking

Tracks timestamps of in-process settings-file writes so the file watcher in ``changeDetector`` can
ignore its own echoes. Extracted from ``changeDetector`` to break the
``settings -> changeDetector -> hooks -> ... -> settings`` import cycle; the timestamp map is the
only shared state.

Callers pass resolved (absolute) paths. The path->source resolution lives in ``settings``, which does
it before calling here. No intra-repo imports (mirrors the TS module — "No imports").

``Date.now()`` (epoch milliseconds) -> ``time.time() * 1000`` so the millisecond ``window_ms`` window
matches the TS semantics exactly.
"""

from __future__ import annotations

import time

# Resolved path -> last in-process-write timestamp (epoch milliseconds).
_timestamps: dict[str, float] = {}


def _now_ms() -> float:
    """Epoch milliseconds."""
    return time.time() * 1000


def mark_internal_write(path: str) -> None:
    """Record that ``path`` is about to be written by this process."""
    _timestamps[path] = _now_ms()


def consume_internal_write(path: str, window_ms: float) -> bool:
    """True if ``path`` was marked within ``window_ms``.

    Consumes the mark on match — the watcher fires once per write, so a matched mark should not
    suppress the next (real, external) change to the same file.
    """
    ts = _timestamps.get(path)
    if ts is not None and _now_ms() - ts < window_ms:
        del _timestamps[path]
        return True
    return False


def clear_internal_writes() -> None:
    """Drop all recorded write marks."""
    _timestamps.clear()
