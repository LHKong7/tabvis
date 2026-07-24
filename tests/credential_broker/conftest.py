"""Shared fakes for Broker/Executor tests — a restricted AuthenticationBrowser that can never
observe secret bytes (only (role, length))."""

from __future__ import annotations

import pytest

from tabvis.authentication.adapters.base import (
    AuthenticationFieldHandle,
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
)
from tabvis.authentication.models import BrowserAuthenticationContext, CredentialProfile


class FakeAuthBrowser:
    def __init__(
        self,
        *,
        available_roles: set[str] | None = None,
        succeeds: bool = True,
        context: BrowserAuthenticationContext | None = None,
    ) -> None:
        self._available = available_roles or {"username", "password", "submit"}
        self._succeeds = succeeds
        self._context = context or default_context()
        self.typed: list[tuple[str, int]] = []
        self.activated: list[str] = []
        self.cleared = 0
        self.waited = 0

    def set_context(self, context: BrowserAuthenticationContext) -> None:
        self._context = context

    async def inspect_context(self) -> BrowserAuthenticationContext:
        return self._context

    async def locate_authentication_field(self, role, hints: AuthenticationFieldHints):
        if role in self._available:
            return AuthenticationFieldHandle(handle_id=f"h_{role}", role=role)
        return None

    async def type_secret(self, field: AuthenticationFieldHandle, value) -> None:
        self.typed.append((field.role, len(bytes(value.borrow_bytes()))))

    async def activate(self, field: AuthenticationFieldHandle) -> None:
        self.activated.append(field.role)

    async def clear_authentication_fields(self) -> None:
        self.cleared += 1

    async def wait_for_authentication_signal(self, condition: AuthenticationSuccessCondition) -> bool:
        self.waited += 1
        return self._succeeds


def default_context(**overrides) -> BrowserAuthenticationContext:
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


def default_profile(**overrides) -> CredentialProfile:
    base = dict(
        id="p1",
        owner_user_id="u1",
        allowed_origins=["https://accounts.example.com"],
        allowed_frame_origins=["https://accounts.example.com"],
        authentication_adapter="generic_password_v1",
        approval_policy="never",
        username_secret_ref="sec_user",
        password_secret_ref="sec_pass",
    )
    base.update(overrides)
    return CredentialProfile(**base)


@pytest.fixture
def browser_cls():
    return FakeAuthBrowser


@pytest.fixture
def make_context():
    return default_context


@pytest.fixture
def make_profile():
    return default_profile
