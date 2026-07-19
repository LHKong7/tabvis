"""Phase 3 — observation pipeline & semantic intents (ROADMAP.md).

Covers OBS-2 (EventBus, gated), OBS-3/4 (producer → normalizer → timeline → re-publish), INT-1/3/4
(engine handlers, IntentRouter, policy-guarded intents), and INT-2 (the flag-gated BrowserIntent
tool). Everything is off by default (``TABVIS_BROWSER_EVENT_BUS`` / ``TABVIS_BROWSER_INTENTS``), so the
tests toggle the flags explicitly. No real browser is launched — the navigation path is exercised
through its PolicyCheck (which blocks before any launch).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import tabvis.browser.event_bus as eb
import tabvis.browser.observation as obs
from tabvis.browser.events import ObservationType, RawEventType, RuntimeEvent
from tabvis.browser.intents import Intent, get_execution_engine, get_intent_router
from tabvis.browser.intents.engine import _search_url
from tabvis.browser.intents.router import is_browser_intents_enabled


@pytest.fixture(autouse=True)
def _reset_bus() -> Any:
    for reset in (_reset,):
        reset()
    yield
    _reset()


def _reset() -> None:
    eb._bus = None
    obs._installed = False
    obs._timeline.clear()


# --------------------------------------------------------------------------- OBS-2: EventBus


def test_event_bus_disabled_is_noop() -> None:
    assert eb.is_event_bus_enabled() is False
    seen: list[RuntimeEvent] = []

    async def sink(ev: RuntimeEvent) -> None:
        seen.append(ev)

    bus = eb.get_event_bus()
    bus.subscribe(sink)
    asyncio.run(bus.publish(RuntimeEvent(type="page.loaded", source="runtime")))
    assert seen == []  # publish is a no-op with the flag off


def test_event_bus_delivers_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_EVENT_BUS", "1")
    assert eb.is_event_bus_enabled() is True
    seen: list[str] = []

    async def sink(ev: RuntimeEvent) -> None:
        seen.append(ev.type)

    bus = eb.get_event_bus()
    bus.subscribe(sink)
    asyncio.run(bus.publish(RuntimeEvent(type="page.loaded", source="runtime")))
    assert seen == ["page.loaded"]


# --------------------------------------------------------------------------- OBS-4: normalizer


def test_normalize_maps_raw_to_semantic() -> None:
    nav = RuntimeEvent(type=RawEventType.ACTION_PERFORMED, source="playwright",
                       payload={"event_type": "navigation", "action": "goto", "url": "https://a.com"})
    assert obs.normalize(nav).type == ObservationType.NAVIGATION_PERFORMED

    click = RuntimeEvent(type=RawEventType.ACTION_PERFORMED, source="playwright",
                         payload={"event_type": "interaction", "action": "click"})
    assert obs.normalize(click).type == ObservationType.ELEMENT_INTERACTED

    search = RuntimeEvent(type=RawEventType.ACTION_PERFORMED, source="playwright",
                          payload={"intent": "search", "url": "https://duckduckgo.com/?q=x"})
    assert obs.normalize(search).type == ObservationType.SEARCH_PERFORMED

    page = RuntimeEvent(type=RawEventType.ACTION_PERFORMED, source="playwright", payload={"action": "snapshot"})
    assert obs.normalize(page).type == ObservationType.PAGE_LOADED

    # A non-raw (already-semantic) event is not re-normalized — this is what keeps the loop finite.
    assert obs.normalize(RuntimeEvent(type=ObservationType.PAGE_LOADED, source="runtime")) is None


def test_pipeline_normalizes_to_timeline_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_EVENT_BUS", "1")
    obs.install_observation_pipeline()

    forwarded: list[str] = []

    async def forwarding_sink(ev: RuntimeEvent) -> None:
        if ev.type in obs.OBSERVATION_TYPES:
            forwarded.append(ev.type)

    eb.get_event_bus().subscribe(forwarding_sink)

    raw = RuntimeEvent(
        type=RawEventType.ACTION_PERFORMED, source="playwright", agent_id="ag_obs",
        payload={"event_type": "navigation", "action": "goto", "url": "https://a.com", "title": "A"},
    )
    asyncio.run(eb.get_event_bus().publish(raw))

    # Normalizer appended one semantic observation to the agent's timeline...
    timeline = obs.get_timeline("ag_obs")
    assert len(timeline) == 1 and timeline[0]["type"] == ObservationType.NAVIGATION_PERFORMED
    assert timeline[0]["url"] == "https://a.com"
    # ...and re-published it so the forwarding sink (OBS-5's shape) received exactly one observation.
    assert forwarded == [ObservationType.NAVIGATION_PERFORMED]


# --------------------------------------------------------------------------- INT-1/3/4: engine + router


def test_engine_registers_all_intent_handlers() -> None:
    assert get_execution_engine().handler_names() == ["compare", "download", "navigate", "research", "search"]


def test_search_url_builder() -> None:
    assert _search_url("hello world") == "https://duckduckgo.com/?q=hello+world"
    assert _search_url("x", "bing").startswith("https://www.bing.com/search?q=")
    assert _search_url("x", "google").startswith("https://www.google.com/search?q=")


def test_download_intent_is_a_seam() -> None:
    rec = asyncio.run(get_execution_engine().run(Intent(name="download")))
    assert rec.status == "failed"
    assert "download" in (rec.error or "").lower()


def test_compare_requires_urls() -> None:
    rec = asyncio.run(get_execution_engine().run(Intent(name="compare", params={"urls": []})))
    assert rec.status == "failed" and "urls" in (rec.error or "")


def test_router_navigate_blocked_by_policy_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Allowlist that the target does not match → the handler's PolicyCheck blocks before any launch.
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    rec = asyncio.run(
        get_intent_router().route(
            Intent(name="navigate", params={"url": "https://evil.test/x"}), agent_id="ag_router"
        )
    )
    assert rec.status == "blocked"
    assert rec.execution_id.startswith("exec_")


def test_router_search_also_policy_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    # Search decomposes to a navigation, which is policy-guarded too (unlike the low-level path).
    monkeypatch.setenv("TABVIS_BROWSER_ALLOWED_DOMAINS", "example.com")
    rec = asyncio.run(
        get_intent_router().route(Intent(name="search", params={"query": "cats"}), agent_id="ag_s")
    )
    assert rec.status == "blocked"  # duckduckgo.com is not in the allowlist


# --------------------------------------------------------------------------- INT-2: BrowserIntent tool


def test_intents_flag_default_off() -> None:
    assert is_browser_intents_enabled() is False


def test_browser_intent_tool_is_flag_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.agent.tools import get_all_base_tools, get_tools_for_default_preset

    assert "BrowserIntent" in [t.name for t in get_all_base_tools()]   # registered
    assert "BrowserIntent" not in get_tools_for_default_preset()       # but filtered off by default

    monkeypatch.setenv("TABVIS_BROWSER_INTENTS", "1")
    assert is_browser_intents_enabled() is True
    assert "BrowserIntent" in get_tools_for_default_preset()           # exposed with the flag on
