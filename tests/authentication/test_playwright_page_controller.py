"""Real Playwright smoke test for the Phase 6 PageController."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.authentication.adapters.base import AuthenticationFieldHints
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.browser.playwright_auth import PlaywrightPageController


class _Service:
    def __init__(self, page) -> None:
        self.active_page = page

    async def close(self) -> None:
        await self.active_page.context.close()


def test_real_playwright_controller_binds_and_invalidates_handles() -> None:
    async def scenario():
        async_api = pytest.importorskip("playwright.async_api")
        async with async_api.async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:  # pragma: no cover - developer has not installed Chromium
                pytest.skip(f"Playwright Chromium unavailable: {type(exc).__name__}")
            context = await browser.new_context()
            page = await context.new_page()

            async def fulfill(route):
                await route.fulfill(
                    content_type="text/html",
                    body=(
                        "<form><input autocomplete='username'>"
                        "<input type='password'><button type='submit'>Sign in</button></form>"
                    ),
                )

            await page.route("https://accounts.example.com/**", fulfill)
            await page.goto("https://accounts.example.com/login")
            controller = PlaywrightPageController(_Service(page), browser_session_id="browser-1")
            auth_context = await controller.current_context()
            assert auth_context.top_level_origin == "https://accounts.example.com"
            assert auth_context.frame_origin == "https://accounts.example.com"
            assert auth_context.is_https

            password = await controller.find_field("password", AuthenticationFieldHints())
            assert password is not None
            await controller.type_bytes(password, b"temporary-password")
            assert await page.locator("input[type='password']").input_value() == "temporary-password"
            await controller.clear_fields()
            assert await page.locator("input[type='password']").input_value() == ""

            stale = await controller.find_field("password", AuthenticationFieldHints())
            await page.goto("https://accounts.example.com/other")
            with pytest.raises(AuthenticationError) as error:
                await controller.activate(stale)
            assert error.value.code is AuthErrorCode.PAGE_CHANGED
            await browser.close()

    asyncio.run(scenario())
