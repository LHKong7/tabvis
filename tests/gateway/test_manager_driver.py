"""BrowserDriver → real browser subsystem bridge (design §10).

`ManagerBrowserDriver` translates the runtime's binding calls into tabvis manager / `BrowserService`
calls. Here every manager touchpoint is an injected fake, so the driver is exercised end to end through
the real `BrowserRuntime` — acquire, navigate, snapshot, verify, close — without launching Chromium.
"""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime.browser.contracts import BrowserAcquireRequest, BrowserIntent
from tabvis.gateway.runtime.browser.manager_driver import ManagerBrowserDriver, shape_observation
from tabvis.gateway.runtime.browser.runtime import BrowserRuntime


class _FakeService:
    """A stand-in BrowserService returning observe()-shaped payloads."""

    def __init__(self) -> None:
        self.navigations: list[tuple[str, str]] = []
        self.snapshots = 0

    async def navigate(self, url: str, *, action: str = "goto", wait_until: str = "load") -> dict:
        self.navigations.append((action, url))
        return {"url": url, "title": "Example", "snapshot": "- link 'Home'", "tab_count": 1}

    async def snapshot(self, *, include_screenshot: bool = False) -> dict:
        self.snapshots += 1
        out = {"url": "https://ex.com", "title": "Example", "snapshot": "- heading 'Hi'"}
        if include_screenshot:
            out["screenshot_b64"] = "QUJD"  # 'ABC'
        return out


class _FakeManager:
    """Records init/verify/close and hands out a shared fake service per agent."""

    def __init__(self) -> None:
        self.initialized: list[str] = []
        self.closed: list[str] = []
        self.services: dict[str, _FakeService] = {}
        self.alive: set[str] = set()

    def init(self, spec, model, cwd) -> None:
        self.initialized.append(spec.agent_id)
        self.alive.add(spec.agent_id)

    async def service(self, agent_id: str) -> _FakeService:
        return self.services.setdefault(agent_id, _FakeService())

    def verify(self, agent_id: str) -> bool:
        return agent_id in self.alive

    async def close(self, agent_id: str) -> bool:
        self.closed.append(agent_id)
        self.alive.discard(agent_id)
        return True


def _driver(mgr: _FakeManager) -> ManagerBrowserDriver:
    return ManagerBrowserDriver(
        initializer=mgr.init, service_provider=mgr.service, verifier=mgr.verify, closer=mgr.close,
    )


def test_shape_observation_maps_the_service_payload() -> None:
    obs = {"url": "u", "title": "t", "snapshot": "aria", "html": "<html>", "screenshot_b64": "b64"}
    shaped = shape_observation(obs)
    assert shaped == {"url": "u", "title": "t", "dom": "<html>", "screenshot": "b64"}
    # falls back to the aria snapshot when there is no raw html.
    assert shape_observation({"snapshot": "aria"})["dom"] == "aria"


def test_acquire_launches_through_the_manager() -> None:
    async def scenario() -> None:
        mgr = _FakeManager()
        rt = BrowserRuntime(driver=_driver(mgr))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_1"))
        assert mgr.initialized == ["ag_1"]           # init_browser_session was called for the agent
        assert b.binding_id

    asyncio.run(scenario())


def test_execute_navigate_drives_the_service_and_records_artifact() -> None:
    async def scenario() -> None:
        mgr = _FakeManager()
        rt = BrowserRuntime(driver=_driver(mgr))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_1"))
        rec = await rt.execute(b.binding_id, BrowserIntent(action="navigate", params={"url": "https://ex.com"}))

        assert rec.status == "succeeded"
        assert mgr.services["ag_1"].navigations == [("goto", "https://ex.com")]
        assert rec.artifact is not None and rec.artifact.ref.startswith("blob:")

        nav = [e for e in get_event_store().read(aggregate_id=b.binding_id)
               if e.type == EventType.BROWSER_NAVIGATION_COMPLETED]
        assert len(nav) == 1 and nav[0].data["url"] == "https://ex.com"
        assert rt.snapshot(b.binding_id).current_url == "https://ex.com"

    asyncio.run(scenario())


def test_execute_snapshot_with_screenshot_stores_artifact_not_base64_in_events() -> None:
    async def scenario() -> None:
        mgr = _FakeManager()
        rt = BrowserRuntime(driver=_driver(mgr))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_1"))
        rec = await rt.execute(b.binding_id, BrowserIntent(action="snapshot", params={"screenshot": True}))
        assert mgr.services["ag_1"].snapshots == 1
        # the screenshot became a stored artifact; its base64 never appears in any event payload.
        assert any(a.type == "screenshot" for a in [rec.artifact] if a)
        for e in get_event_store().read(aggregate_id=b.binding_id):
            assert "QUJD" not in str(e.data)

    asyncio.run(scenario())


def test_verify_identity_and_close_go_through_the_manager() -> None:
    async def scenario() -> None:
        mgr = _FakeManager()
        rt = BrowserRuntime(driver=_driver(mgr))
        b = await rt.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_1"))
        rt.disconnect(b.binding_id)
        assert await rt.reconnect(b.binding_id) is True   # verify_identity → agent is alive
        await rt.close_identity(b.binding_id)
        assert mgr.closed == ["ag_1"]

    asyncio.run(scenario())


def test_execute_on_unlaunched_profile_raises() -> None:
    async def scenario() -> None:
        driver = ManagerBrowserDriver(
            initializer=lambda *a: None, service_provider=lambda aid: _FakeService(),
        )
        from tabvis.gateway.protocol.errors import GatewayError

        with pytest.raises(GatewayError) as ei:
            await driver.execute("profile:never", BrowserIntent(action="snapshot"))
        assert ei.value.code == "BROWSER_BINDING_NOT_FOUND"

    asyncio.run(scenario())
