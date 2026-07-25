"""Deterministic browser-operation protocol tests (no browser process required)."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest
from pydantic import ValidationError

from tabvis.agent.tools.browser_click_tool import BrowserClickInput
from tabvis.agent.tools.browser_keys_tool import BrowserKeysInput
from tabvis.agent.tools.browser_scroll_tool import BrowserScrollInput
from tabvis.browser.browser_service import (
    BrowserError,
    BrowserService,
    _normalize_key_sequence,
    _visible_box_center,
)
from tabvis.browser.policy_guard import evaluate


def test_visible_box_center_uses_visible_intersection() -> None:
    # The element starts outside the viewport; click the visible half, not its off-screen centre.
    assert _visible_box_center(
        {"x": -20, "y": 10, "width": 40, "height": 30}, 100, 80
    ) == (10, 25)
    assert _visible_box_center(
        {"x": 120, "y": 10, "width": 20, "height": 20}, 100, 80
    ) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Enter", "Enter"),
        ("esc", "Escape"),
        ("ctrl+a", "Control+A"),
        ("command+a", "Meta+A"),
        ("Shift+Tab", "Shift+Tab"),
        ("pageDown", "PageDown"),
    ],
)
def test_key_sequences_are_canonicalized(raw: str, expected: str) -> None:
    assert _normalize_key_sequence(raw) == expected


@pytest.mark.parametrize("raw", ["", "Control", "A+B", "Control++A", "Hyper+A"])
def test_invalid_key_sequences_are_rejected(raw: str) -> None:
    with pytest.raises(BrowserError):
        _normalize_key_sequence(raw)


def test_click_schema_requires_exactly_one_target() -> None:
    assert BrowserClickInput(ref="e3").ref == "e3"
    coordinate = BrowserClickInput(coordinate_x=12, coordinate_y=34)
    assert (coordinate.coordinate_x, coordinate.coordinate_y) == (12, 34)
    with pytest.raises(ValidationError):
        BrowserClickInput()
    with pytest.raises(ValidationError):
        BrowserClickInput(ref="e3", coordinate_x=12, coordinate_y=34)
    with pytest.raises(ValidationError):
        BrowserClickInput(coordinate_x=12)


def test_scroll_and_keys_schemas_validate_at_the_boundary() -> None:
    assert BrowserScrollInput(pages=1.5, down=False).pages == 1.5
    assert BrowserKeysInput(keys="Control+A").keys == "Control+A"
    with pytest.raises(ValidationError):
        BrowserScrollInput(pages=0)
    with pytest.raises(ValidationError):
        BrowserKeysInput(keys="Control")


def test_new_actions_are_registered_and_policy_routed() -> None:
    from tabvis.agent.tools import get_all_base_tools

    names = {tool.name for tool in get_all_base_tools()}
    assert {"BrowserScroll", "BrowserKeys"} <= names
    assert evaluate("BrowserScroll", {"pages": 1}, None)["behavior"] == "allow"
    assert evaluate("BrowserKeys", {"keys": "Enter"}, None)["behavior"] == "allow"


class _FailingContext:
    async def new_cdp_session(self, page: object) -> object:
        raise RuntimeError("CDP unavailable")


class _FailingMouse:
    async def move(self, x: float, y: float) -> None:
        raise RuntimeError("native input unavailable")

    async def wheel(self, x: float, y: float) -> None:  # pragma: no cover - move fails first
        raise AssertionError("unreachable")


class _FallbackPage:
    context = _FailingContext()
    mouse = _FailingMouse()

    def __init__(self) -> None:
        self.scrolls: list[float] = []

    async def evaluate(self, script: str, pixels: float) -> None:
        self.scrolls.append(pixels)


def test_page_scroll_reaches_javascript_fallback() -> None:
    service = BrowserService()
    page = _FallbackPage()
    mechanism = asyncio.run(
        service._scroll_once(page, 500, point=(50, 40), locator=None)  # type: ignore[arg-type]
    )
    assert mechanism == "javascript"
    assert page.scrolls == [500]


class _Tab:
    def __init__(self, url: str) -> None:
        self.url = url

    def is_closed(self) -> bool:
        return False

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None


class _TargetBlankLocator:
    async def evaluate(self, script: str) -> bool:
        assert "target" in script and "window" in script
        return True


def test_target_blank_hint_is_detected() -> None:
    assert asyncio.run(BrowserService._opens_new_page(_TargetBlankLocator())) is True  # type: ignore[arg-type]


def test_observe_action_waits_for_delayed_popup_and_switches_to_it() -> None:
    async def scenario() -> tuple[dict, bool, bool, BrowserService, _Tab]:
        service = BrowserService()
        old = _Tab("https://example.test/list")
        popup = _Tab("https://example.test/report")
        context = SimpleNamespace(pages=[old])
        service._context = context  # type: ignore[assignment]
        service._active_page = old  # type: ignore[assignment]

        async def observe(_self: BrowserService, *, include_screenshot: bool = False) -> dict:
            assert include_screenshot is False
            return {"url": _self.active_page.url}

        service.observe = MethodType(observe, service)  # type: ignore[method-assign]

        async def open_later() -> None:
            await asyncio.sleep(0.2)
            context.pages.append(popup)

        task = asyncio.create_task(open_later())
        result = await service._observe_action_change(
            old, old.url, {id(old)}, expect_new_page=True  # type: ignore[arg-type]
        )
        await task
        return *result, service, popup

    data, changed, new_tab, service, popup = asyncio.run(scenario())
    assert (changed, new_tab) == (True, True)
    assert data["url"] == popup.url
    assert service.active_page is popup
