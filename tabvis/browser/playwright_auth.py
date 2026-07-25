"""Playwright-backed restricted authentication controller (Phase 6).

This adapter lives inside the Browser Host side of the trust boundary.  It translates the deliberately
small :class:`tabvis.browser.auth_browser.PageController` protocol onto the live
:class:`tabvis.browser.browser_service.BrowserService` without exposing Playwright objects, DOM,
cookies, storage state, screenshots, or field values to the Agent/Broker-facing surface.

The selectors below are fixed, semantic login-field heuristics.  They are not model supplied.  Every
opaque handle is bound to the page and navigation generation that minted it and is invalidated on a
navigation.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass
from typing import Any

from tabvis.authentication.adapters.base import (
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
    FieldRole,
)
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext
from tabvis.authentication.policy import OriginError, canonicalize_origin

_ROLE_SELECTORS: dict[FieldRole, tuple[str, ...]] = {
    "username": (
        "input[autocomplete='username']",
        "input[type='email']",
        "input[name*='user' i]",
        "input[name*='email' i]",
        "input[id*='user' i]",
        "input[id*='email' i]",
    ),
    "password": (
        "input[autocomplete='current-password']",
        "input[type='password']",
        "input[name*='pass' i]",
        "input[id*='pass' i]",
    ),
    "totp": (
        "input[autocomplete='one-time-code']",
        "input[name*='totp' i]",
        "input[name*='otp' i]",
        "input[name*='code' i]",
        "input[id*='totp' i]",
        "input[id*='otp' i]",
    ),
    "submit": (
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Sign in')",
        "button:has-text('Log in')",
        "button:has-text('Continue')",
        "button:has-text('Next')",
    ),
}

_SENSITIVE_INPUT_SELECTOR = ", ".join(
    (*_ROLE_SELECTORS["username"], *_ROLE_SELECTORS["password"], *_ROLE_SELECTORS["totp"])
)


@dataclass
class _Handle:
    locator: Any
    role: FieldRole
    page_id: str
    generation: int


class PlaywrightPageController:
    """Restricted authentication primitives over one live ``BrowserService``."""

    def __init__(self, service: Any, *, browser_session_id: str) -> None:
        self._service = service
        self._browser_session_id = browser_session_id
        self._page: Any = None
        self._frame: Any = None
        self._page_id = ""
        self._generation = 0
        self._handles: dict[str, _Handle] = {}
        self._typed_generation: int | None = None
        self._typed_page_id: str | None = None

    async def current_context(self) -> BrowserAuthenticationContext:
        page = self._service.active_page
        self._bind_page(page)
        frame = await self._select_authentication_frame(page)
        self._frame = frame

        top_url = page.url
        top_origin = _origin(top_url)
        frame_url = frame.url
        frame_origin = _origin(frame_url, fallback=top_origin)
        ancestors = _ancestor_origins(frame, top_origin=top_origin)
        is_https = top_origin.startswith("https://") and frame_origin.startswith("https://")

        return BrowserAuthenticationContext(
            browser_session_id=self._browser_session_id,
            top_level_url=top_url,
            top_level_origin=top_origin,
            frame_url=frame_url,
            frame_origin=frame_origin,
            ancestor_frame_origins=ancestors,
            is_https=is_https,
            # Playwright rejects certificate errors by default.  The managed-auth composition does
            # not enable ignore_https_errors; a non-HTTPS/effective-origin page is rejected above.
            certificate_valid=is_https,
            navigation_generation=self._generation,
            page_id=self._page_id,
        )

    async def find_field(
        self, role: FieldRole, hints: AuthenticationFieldHints
    ) -> str | None:
        del hints  # fixed semantic heuristics only; never turn model/site text into a selector
        page = self._service.active_page
        self._bind_page(page)
        frame = self._frame or await self._select_authentication_frame(page)
        locator = await _first_visible(frame, _ROLE_SELECTORS[role])
        if locator is None:
            return None
        handle_id = "authfield_" + uuid.uuid4().hex[:16]
        self._handles[handle_id] = _Handle(
            locator=locator,
            role=role,
            page_id=self._page_id,
            generation=self._generation,
        )
        return handle_id

    async def type_bytes(self, handle_id: str, data: bytes) -> None:
        handle = self._valid_handle(handle_id)
        # Playwright's public fill API accepts text, so a short-lived str is unavoidable inside the
        # trusted Browser Host.  It is never logged, returned, placed in a tool argument, or retained.
        text = data.decode("utf-8")
        try:
            await handle.locator.fill(text)
            self._typed_page_id = self._page_id
            self._typed_generation = self._generation
        finally:
            del text

    async def activate(self, handle_id: str) -> None:
        handle = self._valid_handle(handle_id)
        await handle.locator.click()

    async def clear_fields(self) -> None:
        # A committed navigation destroyed the document that held the typed values.  Clearing stale
        # locators is neither possible nor necessary; the old renderer document is gone.
        page = self._service.active_page
        self._bind_page(page)
        if self._typed_page_id is None:
            return
        if self._typed_page_id != self._page_id:
            self._forget_sensitive_handles()
            return
        if self._typed_generation is not None and self._generation != self._typed_generation:
            self._forget_sensitive_handles()
            return

        failures = 0
        for handle in list(self._handles.values()):
            if handle.role == "submit":
                continue
            try:
                await handle.locator.fill("")
            except Exception:  # noqa: BLE001 - caller must destroy the context if clearing is uncertain
                failures += 1
        self._forget_sensitive_handles()
        if failures:
            raise RuntimeError("authentication_field_clear_unconfirmed")

    async def check_signal(self, condition: AuthenticationSuccessCondition) -> bool:
        deadline = time.monotonic() + max(0.1, condition.timeout_seconds)
        while time.monotonic() < deadline:
            page = self._service.active_page
            self._bind_page(page)
            try:
                if condition.kind == "cookie_present" and condition.cookie_name:
                    cookies = await page.context.cookies([page.url])
                    if any(cookie.get("name") == condition.cookie_name for cookie in cookies):
                        return True
                elif condition.kind in ("dom_condition", "logged_in_ui") and condition.marker:
                    marker = page.get_by_test_id(condition.marker)
                    if await marker.count() and await marker.first.is_visible():
                        return True
                elif condition.kind == "logged_in_ui":
                    # Stronger than a URL change: the credential form must have disappeared.
                    has_sensitive_field = False
                    for frame in page.frames:
                        if await frame.locator(_SENSITIVE_INPUT_SELECTOR).count():
                            has_sensitive_field = True
                            break
                    if not has_sensitive_field:
                        return True
                # Generic account_api_ok deliberately has no implementation: a site adapter must
                # provide a host-side allowlisted endpoint rather than letting a model choose a URL.
            except Exception:  # noqa: BLE001 - navigation races are retried until the deadline
                pass
            await asyncio.sleep(0.1)
        return False

    async def storage_state(self) -> dict[str, Any]:
        """Export storage state inside the Browser Host for Session Vault persistence."""
        page = self._service.active_page
        return await page.context.storage_state()

    async def restore_storage_state(self, storage_state: dict[str, Any]) -> None:
        """Restore Vault state inside the Browser Host; no cookie/storage value is returned."""
        page = self._service.active_page
        cookies = storage_state.get("cookies", [])
        if isinstance(cookies, list) and cookies:
            await page.context.add_cookies(
                [cookie for cookie in cookies if isinstance(cookie, dict)]
            )

        current_origin = _origin(page.url)
        for origin_state in storage_state.get("origins", []):
            if not isinstance(origin_state, dict) or origin_state.get("origin") != current_origin:
                continue
            entries = origin_state.get("localStorage", [])
            if isinstance(entries, list):
                await page.evaluate(
                    """entries => {
                        for (const entry of entries) {
                            if (entry && typeof entry.name === "string"
                                && typeof entry.value === "string") {
                                localStorage.setItem(entry.name, entry.value);
                            }
                        }
                    }""",
                    entries,
                )
        # Re-evaluate the current route with the restored cookies/storage. Any redirect/navigation
        # increments the generation and invalidates pre-restore field handles.
        await page.reload(wait_until="domcontentloaded")

    def arm_post_auth_redaction(self) -> None:
        self._service.arm_post_auth_redaction()

    async def destroy_context(self) -> None:
        """Destroy the live context when field cleanup cannot be confirmed."""
        await self._service.close()

    def exclusive_control(self):
        """Use the BrowserService's action lane for the complete authentication transaction."""
        return self._service.authentication_control()

    # ----------------------------------------------------------------------------------

    def _bind_page(self, page: Any) -> None:
        if page is self._page:
            return
        self._page = page
        self._frame = None
        self._page_id = "page_" + uuid.uuid4().hex[:16]
        self._generation = 0
        self._handles.clear()

        def _on_navigation(_frame: Any) -> None:
            self._generation += 1
            self._handles.clear()

        page.on("framenavigated", _on_navigation)

    async def _select_authentication_frame(self, page: Any) -> Any:
        if self._frame is not None and self._frame in page.frames:
            return self._frame
        # Prefer the frame that contains a password, then one with a username.  This lets the Broker
        # authorize the actual input frame and its complete ancestor chain before any secret is read.
        for role in ("password", "username"):
            for frame in page.frames:
                with contextlib.suppress(Exception):
                    if await frame.locator(", ".join(_ROLE_SELECTORS[role])).count():
                        return frame
        return page.main_frame

    def _valid_handle(self, handle_id: str) -> _Handle:
        handle = self._handles.get(handle_id)
        if (
            handle is None
            or handle.page_id != self._page_id
            or handle.generation != self._generation
        ):
            raise AuthenticationError(AuthErrorCode.PAGE_CHANGED)
        return handle

    def _forget_sensitive_handles(self) -> None:
        self._handles = {key: value for key, value in self._handles.items() if value.role == "submit"}
        self._typed_generation = None
        self._typed_page_id = None


async def _first_visible(frame: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        try:
            locator = frame.locator(selector)
            count = min(await locator.count(), 5)
            for index in range(count):
                candidate = locator.nth(index)
                if await candidate.is_visible() and await candidate.is_enabled():
                    return candidate
        except Exception:  # noqa: BLE001 - one unsupported selector/frame must not abort discovery
            continue
    return None


def _origin(url: str, *, fallback: str | None = None) -> str:
    if url in ("about:blank", "about:srcdoc") and fallback:
        return fallback
    try:
        return canonicalize_origin(url)
    except OriginError:
        return ""


def _ancestor_origins(frame: Any, *, top_origin: str) -> list[str]:
    out: list[str] = []
    current = frame.parent_frame
    while current is not None:
        origin = _origin(current.url, fallback=top_origin)
        if origin:
            out.append(origin)
        current = current.parent_frame
    return out
