"""Browser request pacing — the agent must not burst / DoS a host.

Verifies host_of()'s exemptions and that RequestPacer enforces per-host intervals, the global gap,
and the per-minute ceiling. Uses asyncio.run (the repo has no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from tabvis.browser.rate_limiter import (
    DEFAULT_MAX_REQUESTS_PER_MINUTE,
    DEFAULT_MIN_ACTION_INTERVAL_MS,
    DEFAULT_MIN_REQUEST_INTERVAL_MS,
    DEFAULT_REQUEST_JITTER_MS,
    RequestPacer,
    RequestPacingError,
    host_of,
)


@pytest.fixture(autouse=True)
def _deterministic_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_REQUEST_JITTER_MS", "0")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/path", "example.com"),
        ("https://SUB.Example.COM", "sub.example.com"),
        ("http://127.0.0.1:8080/x", None),      # loopback exempt
        ("http://localhost:5173", None),        # loopback exempt
        ("data:text/html,<h1>hi</h1>", None),   # host-less exempt
        ("about:blank", None),
        ("", None),
        (None, None),
    ],
)
def test_host_of(url, expected) -> None:
    assert host_of(url) == expected


def _elapsed(coro_factory) -> float:
    async def _run() -> float:
        t0 = time.monotonic()
        await coro_factory()
        return time.monotonic() - t0

    return asyncio.run(_run())


def test_per_host_interval_spaces_same_host(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "200")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "0")
    monkeypatch.setenv("TABVIS_BROWSER_REQUEST_JITTER_MS", "0")

    async def two_hits() -> None:
        p = RequestPacer()
        await p.pace("example.com", counts_as_request=True)   # first: no wait
        await p.pace("example.com", counts_as_request=True)   # second: ~200ms later

    assert _elapsed(two_hits) >= 0.18  # ~0.2s spacing (small slack)


def test_different_hosts_not_serialized(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "500")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")

    async def two_hosts() -> None:
        p = RequestPacer()
        await p.pace("a.com", counts_as_request=True)
        await p.pace("b.com", counts_as_request=True)  # different host — no wait

    assert _elapsed(two_hosts) < 0.15


def test_loopback_and_hostless_not_paced(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "500")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")

    async def loopback() -> None:
        p = RequestPacer()
        await p.pace(host_of("http://127.0.0.1:9/x"), counts_as_request=True)
        await p.pace(host_of("http://127.0.0.1:9/y"), counts_as_request=True)

    assert _elapsed(loopback) < 0.15


def test_non_request_action_skips_per_host(monkeypatch) -> None:
    """A plain type (counts_as_request=False) is not per-host paced."""
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "500")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")

    async def two_types() -> None:
        p = RequestPacer()
        await p.pace("example.com", counts_as_request=False)
        await p.pace("example.com", counts_as_request=False)

    assert _elapsed(two_types) < 0.15


def test_global_action_interval_applies_to_all(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "200")

    async def two_actions() -> None:
        p = RequestPacer()
        await p.pace(None, counts_as_request=False)  # even a host-less action
        await p.pace(None, counts_as_request=False)

    assert _elapsed(two_actions) >= 0.18


def test_requests_per_minute_ceiling_fails_closed_above_wait_budget(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "2")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_PACING_WAIT_MS", "300")

    async def three_hits() -> None:
        p = RequestPacer()
        await p.pace("example.com", counts_as_request=True)  # 1
        await p.pace("example.com", counts_as_request=True)  # 2
        with pytest.raises(RequestPacingError, match="action was not sent"):
            await p.pace("example.com", counts_as_request=True)

    asyncio.run(three_hits())


def test_future_reservations_do_not_pile_up_at_window_boundary(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "2")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_PACING_WAIT_MS", "180000")

    async def reserve_five() -> list[float]:
        p = RequestPacer()

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        for _ in range(5):
            await p.pace("example.com", counts_as_request=True)
        return list(p._host_hits["example.com"])

    slots = asyncio.run(reserve_five())
    assert slots[0] == pytest.approx(slots[1], abs=0.01)
    assert slots[2] == pytest.approx(slots[0] + 60, abs=0.01)
    assert slots[3] == pytest.approx(slots[1] + 60, abs=0.01)
    assert slots[4] == pytest.approx(slots[2] + 60, abs=0.01)


def test_safe_defaults_are_enabled() -> None:
    assert DEFAULT_MIN_REQUEST_INTERVAL_MS == 1_500
    assert DEFAULT_MAX_REQUESTS_PER_MINUTE == 12
    assert DEFAULT_MIN_ACTION_INTERVAL_MS == 120
    assert DEFAULT_REQUEST_JITTER_MS == 250


def test_server_throttle_creates_shared_fail_closed_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "1")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_PACING_WAIT_MS", "100")

    async def throttled() -> None:
        p = RequestPacer()
        assert await p.note_response("example.com", 429, "90") == 90
        with pytest.raises(RequestPacingError, match="action was not sent"):
            await p.pace("example.com", counts_as_request=True)
        assert await p.note_response("example.com", 200) == 0

    asyncio.run(throttled())


def test_disabled_is_fast(monkeypatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "0")
    monkeypatch.setenv("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "0")

    async def many() -> None:
        p = RequestPacer()
        for _ in range(50):
            await p.pace("example.com", counts_as_request=True)

    assert _elapsed(many) < 0.15
