"""Phase 7 — Browser Runtime: leases, isolation, recovery, artifacts (design §10, §15)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime.browser.contracts import BrowserAcquireRequest, BrowserIntent
from tabvis.gateway.runtime.browser.identity import profile_key_for, resolve_identity
from tabvis.gateway.runtime.browser.runtime import BrowserRuntime
from tabvis.gateway.runtime.browser import session as session_mod


class _Clock:
    """A controllable clock for deterministic lease-expiry tests."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _FakeDriver:
    """Records driver calls; returns deterministic results instead of driving a real browser."""

    def __init__(self, identity_ok: bool = True) -> None:
        self.launched: list[str] = []
        self.closed: list[str] = []
        self.identity_ok = identity_ok

    async def launch(self, spec) -> None:
        self.launched.append(spec.profile_key)

    async def execute(self, profile_key: str, intent: BrowserIntent) -> dict:
        return dict(intent.params)

    async def verify_identity(self, profile_key: str) -> bool:
        return self.identity_ok

    async def close(self, profile_key: str) -> None:
        self.closed.append(profile_key)


def _runtime(clock=None, driver=None, ttl=30.0) -> BrowserRuntime:
    return BrowserRuntime(driver=driver or _FakeDriver(), clock=clock, ttl_seconds=ttl)


# --- identity / profile keys -------------------------------------------------------------------


def test_isolated_and_shared_profile_keys() -> None:
    # isolated agents get distinct keys; a shared named profile collapses to one key (design §10.5).
    assert profile_key_for("ag_a", None) != profile_key_for("ag_b", None)
    assert profile_key_for("ag_a", "work") == profile_key_for("ag_b", "work")


def test_identity_metadata_hides_secrets() -> None:
    ident = resolve_identity("ag_1", None)
    ident.metadata.update({"locale": "en-US", "cookies": "SECRET", "token": "SECRET"})
    public = ident.public_metadata()
    assert public == {"locale": "en-US"}  # cookies/token dropped (design §10.5)


# --- acquire / isolation / conflict ------------------------------------------------------------


def test_two_isolated_agents_run_in_parallel() -> None:
    # design §15 Phase 7 acceptance: two isolated Agents run in parallel.
    async def scenario() -> None:
        rt = _runtime()
        a = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_b", run_id="run_b"))
        assert a.binding_id != b.binding_id
        assert a.profile_key != b.profile_key  # no contention

    asyncio.run(scenario())


def test_shared_profile_conflict_is_deterministic() -> None:
    # design §15 Phase 7 acceptance: shared browser profile conflict is deterministic.
    async def scenario() -> None:
        rt = _runtime()
        await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a", profile="team"))
        with pytest.raises(GatewayError) as ei:
            await rt.acquire(BrowserAcquireRequest(agent_id="ag_b", run_id="run_b", profile="team"))
        assert ei.value.code == "BROWSER_PROFILE_BUSY"

    asyncio.run(scenario())


def test_same_run_reacquire_is_idempotent() -> None:
    async def scenario() -> None:
        rt = _runtime()
        a = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a", profile="team"))
        # the SAME run re-acquiring the profile is idempotent, not a conflict.
        a2 = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a", profile="team"))
        assert a2.binding_id == a.binding_id

    asyncio.run(scenario())


def test_release_frees_the_profile_for_another_run() -> None:
    async def scenario() -> None:
        rt = _runtime()
        a = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a", profile="team"))
        await rt.release(a.binding_id)
        # after release the profile is free — a different run can take it.
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_b", run_id="run_b", profile="team"))
        assert b.binding_id != a.binding_id

    asyncio.run(scenario())


# --- heartbeat / lease recovery ----------------------------------------------------------------


def test_expired_lease_is_reclaimable_but_live_lease_is_not() -> None:
    # design §15 Phase 7 acceptance: crash recovery does not corrupt or silently reassign a live profile.
    async def scenario() -> None:
        clock = _Clock()
        rt = _runtime(clock=clock, ttl=30.0)
        live = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a", profile="p1"))
        stale = await rt.acquire(BrowserAcquireRequest(agent_id="ag_b", run_id="run_b", profile="p2"))

        # keep the "live" lease's heartbeat fresh; let the "stale" one lapse.
        clock.advance(20)
        rt.heartbeat(live.binding_id)
        clock.advance(20)  # now: live heartbeat 20s ago (< ttl), stale 40s ago (> ttl)

        reclaimed = rt.recover()
        assert stale.binding_id in reclaimed
        assert live.binding_id not in reclaimed          # the live profile is never reclaimed
        # the reclaimed profile is now free; the still-held one stays busy.
        await rt.acquire(BrowserAcquireRequest(agent_id="ag_c", run_id="run_c", profile="p2"))
        with pytest.raises(GatewayError):
            await rt.acquire(BrowserAcquireRequest(agent_id="ag_d", run_id="run_d", profile="p1"))

    asyncio.run(scenario())


def test_heartbeat_extends_the_lease() -> None:
    async def scenario() -> None:
        clock = _Clock()
        rt = _runtime(clock=clock, ttl=30.0)
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        clock.advance(25)
        rt.heartbeat(b.binding_id)   # refresh before expiry
        clock.advance(20)            # 20s since refresh (< ttl) → still live
        assert rt.recover() == []    # nothing reclaimed

    asyncio.run(scenario())


# --- execute / artifacts -----------------------------------------------------------------------


def test_navigation_records_a_content_addressed_artifact_not_bytes() -> None:
    async def scenario() -> None:
        rt = _runtime()
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        rec = await rt.execute(b.binding_id, BrowserIntent(
            action="navigate", params={"url": "https://ex.com", "title": "Ex", "dom": "<html>hi</html>"}))
        assert rec.status == "succeeded"
        assert rec.artifact is not None and rec.artifact.ref.startswith("blob:")

        # the navigation event carries the artifact REF, never the DOM bytes (design §10.6).
        nav = [e for e in get_event_store().read(aggregate_id=b.binding_id)
               if e.type == EventType.BROWSER_NAVIGATION_COMPLETED]
        assert len(nav) == 1
        assert nav[0].data["artifact_ref"].startswith("blob:")
        assert "<html>" not in str(nav[0].data)

        snap = rt.snapshot(b.binding_id)
        assert snap.current_url == "https://ex.com"

    asyncio.run(scenario())


def test_download_is_quarantined_with_collision_safe_name() -> None:
    async def scenario() -> None:
        rt = _runtime()
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        r1 = await rt.execute(b.binding_id, BrowserIntent(action="download", side_effecting=True,
                                                          params={"download": {"name": "report.pdf", "bytes": b"a"}}))
        r2 = await rt.execute(b.binding_id, BrowserIntent(action="download", side_effecting=True,
                                                          params={"download": {"name": "report.pdf", "bytes": b"b"}}))
        assert r1.artifact.ref != r2.artifact.ref           # collision-safe
        assert "quarantine/" in r1.artifact.ref
        dl = [e for e in get_event_store().read(aggregate_id=b.binding_id)
              if e.type == EventType.BROWSER_DOWNLOAD_COMPLETED]
        assert len(dl) == 2

    asyncio.run(scenario())


def test_dom_snapshot_is_size_limited() -> None:
    async def scenario() -> None:
        rt = _runtime()
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        huge = "x" * (session_mod.MAX_DOM_BYTES + 1000)
        rec = await rt.execute(b.binding_id, BrowserIntent(action="navigate", params={"url": "u", "dom": huge}))
        assert rec.artifact.truncated is True
        assert rec.artifact.size_bytes == session_mod.MAX_DOM_BYTES

    asyncio.run(scenario())


# --- disconnect / reconnect --------------------------------------------------------------------


def test_side_effecting_execute_while_disconnected_is_interrupted() -> None:
    # design §10.7: uncertain side-effecting executions are marked interrupted, not replayed.
    async def scenario() -> None:
        rt = _runtime()
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        rt.disconnect(b.binding_id)
        rec = await rt.execute(b.binding_id, BrowserIntent(action="submit", side_effecting=True))
        assert rec.status == "interrupted"

    asyncio.run(scenario())


def test_reconnect_verifies_identity_before_resuming() -> None:
    async def scenario() -> None:
        rt = _runtime(driver=_FakeDriver(identity_ok=True))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        rt.disconnect(b.binding_id)
        assert await rt.reconnect(b.binding_id) is True
        assert rt.snapshot(b.binding_id).session_state == session_mod.BUSY

    asyncio.run(scenario())


def test_reconnect_fails_when_identity_cannot_be_verified() -> None:
    async def scenario() -> None:
        rt = _runtime(driver=_FakeDriver(identity_ok=False))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        rt.disconnect(b.binding_id)
        assert await rt.reconnect(b.binding_id) is False
        assert rt.snapshot(b.binding_id).session_state == session_mod.FAILED

    asyncio.run(scenario())


def test_binding_acquired_and_released_events_emitted() -> None:
    async def scenario() -> None:
        rt = _runtime()
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_a", run_id="run_a"))
        await rt.release(b.binding_id)
        types = [e.type for e in get_event_store().read(aggregate_id=b.binding_id)]
        assert EventType.BROWSER_BINDING_ACQUIRED in types
        assert EventType.BROWSER_BINDING_RELEASED in types

    asyncio.run(scenario())
