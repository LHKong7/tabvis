"""Request pacing for browser actions — be a polite client, never a burst.

The agent can fire navigations and clicks far faster than a person, and a tight loop of page loads
against one host is indistinguishable from (and can amount to) a denial-of-service. This paces the
request-causing browser actions so tabvis stays a well-behaved client:

- a minimum interval between request-causing actions to the SAME host,
- a per-host cap of N requests/minute (a hard rolling-window ceiling),
- a global minimum gap between ANY two browser actions,
- jitter, so concurrent agents don't fire in lockstep.
- a shared cooldown after top-level HTTP 403/429/503 responses.

It is process-wide (a module singleton), so several concurrent agents SHARE the per-host limits and
cannot collectively hammer one server. Slots are reserved under a short lock and the wait happens
outside it, so pacing one action never blocks the bookkeeping of another.

Knobs (read per call, like the rest of tabvis config; all times in milliseconds):
  TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS   min gap between requests to one host       (default 1500)
  TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE   per-host rolling ceiling, 0 = off          (default 12)
  TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS    min gap between ANY two actions, 0 = off   (default 120)
  TABVIS_BROWSER_REQUEST_JITTER_MS         random 0..N ms added to each slot          (default 250)
  TABVIS_BROWSER_MAX_PACING_WAIT_MS        safety cap on a single wait                (default 60000)

Loopback hosts (localhost / 127.0.0.1 / ::1) and host-less URLs (data:, about:blank, file:) are
never per-host paced — you cannot DoS your own machine, and this keeps local tests fast.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from tabvis.utils.debug import log_for_debugging

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", ""})
DEFAULT_MIN_REQUEST_INTERVAL_MS = 1_500
DEFAULT_MAX_REQUESTS_PER_MINUTE = 12
DEFAULT_MIN_ACTION_INTERVAL_MS = 120
DEFAULT_REQUEST_JITTER_MS = 250
DEFAULT_MAX_PACING_WAIT_MS = 60_000


class RequestPacingError(RuntimeError):
    """A browser action would exceed the configured safe pacing budget."""


def host_of(url: str | None) -> str | None:
    """The lowercased hostname of ``url``, or ``None`` when there isn't a real remote host."""
    if not url:
        return None
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, TypeError):
        return None
    if host in _LOOPBACK_HOSTS:
        return None
    return host or None


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(float(raw))
    except (ValueError, TypeError):
        return default
    return max(0, value)


class RequestPacer:
    """Serialize-and-space request-causing browser actions across the whole process."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._next_host: dict[str, float] = {}
        self._next_global: float = 0.0
        self._host_hits: dict[str, deque[float]] = defaultdict(deque)
        self._blocked_until: dict[str, float] = {}

    async def note_response(
        self,
        host: str | None,
        status: int | None,
        retry_after: str | None = None,
    ) -> float:
        """Apply a shared host cooldown after explicit server throttling/refusal.

        ``Retry-After`` delta-seconds wins when present. Otherwise 429 cools down for a minute,
        503 for 30 seconds, and 403 for 15 seconds. The latter prevents a blocked research loop
        from repeatedly probing the same page while still allowing a human to correct auth soon.
        Returns the cooldown seconds applied.
        """
        if not host or status not in {403, 429, 503}:
            return 0.0
        defaults = {403: 15.0, 429: 60.0, 503: 30.0}
        cooldown = defaults[status]
        if retry_after:
            try:
                cooldown = max(cooldown, float(retry_after.strip()))
            except (TypeError, ValueError):
                pass
        cooldown = min(cooldown, 3_600.0)
        async with self._lock:
            self._blocked_until[host] = max(
                self._blocked_until.get(host, 0.0),
                time.monotonic() + cooldown,
            )
        log_for_debugging(
            f"[BROWSER] {host} returned HTTP {status}; shared cooldown {cooldown:.0f}s"
        )
        return cooldown

    async def pace(self, host: str | None, *, counts_as_request: bool) -> float:
        """Wait until this action is allowed to proceed. Returns the seconds actually slept.

        ``counts_as_request`` applies the per-host request limits (a navigation loads a page = many
        requests); other interactions only honor the global min-action gap, so filling a form isn't
        needlessly throttled.
        """
        min_action = (
            _int_env(
                "TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS",
                DEFAULT_MIN_ACTION_INTERVAL_MS,
            )
            / 1000.0
        )
        per_host = (
            _int_env(
                "TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS",
                DEFAULT_MIN_REQUEST_INTERVAL_MS,
            )
            / 1000.0
            if counts_as_request
            else 0.0
        )
        rpm = (
            _int_env(
                "TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE",
                DEFAULT_MAX_REQUESTS_PER_MINUTE,
            )
            if counts_as_request
            else 0
        )
        jitter_ms = _int_env(
            "TABVIS_BROWSER_REQUEST_JITTER_MS", DEFAULT_REQUEST_JITTER_MS
        )
        max_wait = (
            _int_env(
                "TABVIS_BROWSER_MAX_PACING_WAIT_MS",
                DEFAULT_MAX_PACING_WAIT_MS,
            )
            / 1000.0
        )

        paced_host = host if (per_host > 0 or rpm > 0) else None
        if min_action <= 0 and paced_host is None:
            return 0.0  # nothing to enforce — fast path

        now = time.monotonic()
        async with self._lock:
            target = now
            if min_action > 0:
                target = max(target, self._next_global)
            if paced_host is not None and per_host > 0:
                target = max(target, self._next_host.get(paced_host, 0.0))
            if paced_host is not None:
                blocked_until = self._blocked_until.get(paced_host, 0.0)
                if blocked_until <= now:
                    self._blocked_until.pop(paced_host, None)
                else:
                    target = max(target, blocked_until)
            if paced_host is not None and rpm > 0:
                hits = self._host_hits[paced_host]
                cutoff = now - 60.0
                while hits and hits[0] < cutoff:
                    hits.popleft()
                if len(hits) >= rpm:
                    # Include already-reserved future slots. Using hits[0] would let every caller
                    # beyond the ceiling pile onto the same timestamp and create a new burst when
                    # the minute turns over. The Nth most recent slot keeps each rolling window at N.
                    target = max(target, hits[-rpm] + 60.0)

            if jitter_ms > 0:
                target += random.uniform(0, jitter_ms / 1000.0)

            required_wait = max(0.0, target - now)
            if required_wait > max_wait:
                raise RequestPacingError(
                    f"Browser action to {host or '(local)'} needs {required_wait:.2f}s of "
                    f"pacing, above TABVIS_BROWSER_MAX_PACING_WAIT_MS={int(max_wait * 1000)}. "
                    "The action was not sent; wait before retrying instead of looping."
                )

            # Reserve this slot so the next caller queues behind it.
            if min_action > 0:
                self._next_global = target + min_action
            if paced_host is not None and per_host > 0:
                self._next_host[paced_host] = target + per_host
            if paced_host is not None and rpm > 0:
                self._host_hits[paced_host].append(target)

            sleep_for = required_wait

        if sleep_for > 0:
            log_for_debugging(
                f"[BROWSER] pacing {'navigation' if counts_as_request else 'action'} "
                f"to {host or '(local)'}: waiting {sleep_for:.2f}s to avoid bursting the server"
            )
            await asyncio.sleep(sleep_for)
        return sleep_for


_pacer: RequestPacer | None = None


def get_request_pacer() -> RequestPacer:
    """The process-wide pacer (shared by every agent so per-host limits are global)."""
    global _pacer
    if _pacer is None:
        _pacer = RequestPacer()
    return _pacer


def _reset_for_test() -> None:
    global _pacer
    _pacer = None
