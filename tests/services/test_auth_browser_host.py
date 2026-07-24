"""Restricted authentication browser + Browser Host gating (design §4.2, §6.4, §7.1, §13)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.authentication.adapters.base import (
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
)
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext
from tabvis.authentication.secrets import secret_from_str
from tabvis.browser import auth_lease, host
from tabvis.browser.auth_browser import RestrictedAuthenticationBrowser


def _ctx(**overrides) -> BrowserAuthenticationContext:
    base = dict(
        browser_session_id="b1",
        top_level_url="https://accounts.example.com/login",
        top_level_origin="https://accounts.example.com",
        frame_url="https://accounts.example.com/login",
        frame_origin="https://accounts.example.com",
        ancestor_frame_origins=[],
        is_https=True,
        certificate_valid=True,
        navigation_generation=1,
        page_id="page-1",
    )
    base.update(overrides)
    return BrowserAuthenticationContext(**base)


class FakePageController:
    """Low-level page fake — records typed byte lengths only, exposes no cookie/DOM/JS surface."""

    def __init__(self, *, context=None, fields=None, signal=True) -> None:
        self._context = context or _ctx()
        self._fields = fields or {"username": "h_u", "password": "h_p", "submit": "h_s"}
        self._signal = signal
        self.typed: list[tuple[str, int]] = []
        self.cleared = 0

    def set_context(self, context) -> None:
        self._context = context

    async def current_context(self):
        return self._context

    async def find_field(self, role, hints: AuthenticationFieldHints):
        return self._fields.get(role)

    async def type_bytes(self, handle_id: str, data: bytes) -> None:
        self.typed.append((handle_id, len(data)))

    async def activate(self, handle_id: str) -> None:
        pass

    async def clear_fields(self) -> None:
        self.cleared += 1

    async def check_signal(self, condition: AuthenticationSuccessCondition) -> bool:
        return self._signal


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- restricted browser


def test_type_secret_passes_bytes_and_revalidates() -> None:
    controller = FakePageController()
    browser = RestrictedAuthenticationBrowser(controller, _ctx())

    async def scenario():
        field = await browser.locate_authentication_field("password", AuthenticationFieldHints())
        await browser.type_secret(field, secret_from_str("hunter2xyz"))

    _run(scenario())
    assert controller.typed == [("h_p", len("hunter2xyz"))]


def test_type_secret_aborts_on_page_change() -> None:
    controller = FakePageController()
    browser = RestrictedAuthenticationBrowser(controller, _ctx())

    async def scenario():
        field = await browser.locate_authentication_field("password", AuthenticationFieldHints())
        # page navigates between binding and typing → drift
        controller.set_context(_ctx(navigation_generation=2))
        await browser.type_secret(field, secret_from_str("hunter2xyz"))

    with pytest.raises(AuthenticationError) as exc:
        _run(scenario())
    assert exc.value.code is AuthErrorCode.PAGE_CHANGED
    assert controller.typed == []  # nothing was typed after the drift


def test_type_secret_aborts_on_origin_change() -> None:
    controller = FakePageController()
    browser = RestrictedAuthenticationBrowser(controller, _ctx())

    async def scenario():
        field = await browser.locate_authentication_field("password", AuthenticationFieldHints())
        controller.set_context(_ctx(top_level_origin="https://evil.test", frame_origin="https://evil.test"))
        await browser.type_secret(field, secret_from_str("hunter2xyz"))

    with pytest.raises(AuthenticationError) as exc:
        _run(scenario())
    assert exc.value.code is AuthErrorCode.ORIGIN_NOT_ALLOWED


# --------------------------------------------------------------------------- host gating


def test_agent_rpc_blocked_during_authentication() -> None:
    assert host.agent_rpc_allowed("b1") is True
    lease = auth_lease.acquire("b1", task_id="t1", request_id="r1")
    try:
        assert host.agent_rpc_allowed("b1") is False
        assert host.agent_rpc_allowed() is False  # any-session form also blocked
        with pytest.raises(PermissionError):
            host.guard_agent_rpc("b1")
    finally:
        lease.release()
    assert host.agent_rpc_allowed("b1") is True


def test_begin_authentication_holds_then_releases_lease() -> None:
    controller = FakePageController()

    async def scenario():
        assert not auth_lease.is_authentication_locked("b1")
        async with host.begin_authentication(
            controller, browser_session_id="b1", task_id="t1", request_id="r1"
        ) as session:
            assert auth_lease.is_authentication_locked("b1")
            assert isinstance(session.browser, RestrictedAuthenticationBrowser)
        # lease released and fields cleared on exit
        assert not auth_lease.is_authentication_locked("b1")
        assert controller.cleared == 1

    _run(scenario())


def test_begin_authentication_marks_destroy_on_clear_failure() -> None:
    controller = FakePageController()

    async def failing_clear():
        raise RuntimeError("clear failed")

    controller.clear_fields = failing_clear  # type: ignore[assignment]

    async def scenario():
        async with host.begin_authentication(
            controller, browser_session_id="b2", task_id="t1", request_id="r1"
        ) as session:
            pass
        return session

    session = _run(scenario())
    assert session.must_destroy_context is True
    assert not auth_lease.is_authentication_locked("b2")  # lease still released
