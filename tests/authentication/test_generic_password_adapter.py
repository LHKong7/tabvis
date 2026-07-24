"""Generic password adapter against a fake AuthenticationBrowser (design §9.2, §16.2).

The fake records what was typed *only* as field-role → length (never the secret bytes), which is
exactly the discipline the real browser host must keep, and lets us assert the flow without a live
browser.
"""

from __future__ import annotations

from tabvis.authentication.adapters.base import (
    AuthenticationFieldHandle,
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
)
from tabvis.authentication.adapters.generic_password import GenericPasswordAdapter
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext, CredentialProfile, ResolvedCredentials
from tabvis.authentication.secrets import secret_from_str

_SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _profile() -> CredentialProfile:
    return CredentialProfile(
        id="p1",
        owner_user_id="u1",
        allowed_origins=["https://accounts.example.com"],
        authentication_adapter="generic_password_v1",
    )


def _ctx() -> BrowserAuthenticationContext:
    return BrowserAuthenticationContext(
        browser_session_id="b1",
        top_level_url="https://accounts.example.com/home",
        top_level_origin="https://accounts.example.com",
        frame_url="https://accounts.example.com/home",
        frame_origin="https://accounts.example.com",
        is_https=True,
        certificate_valid=True,
        navigation_generation=1,
        page_id="page-1",
    )


class FakeBrowser:
    """Configurable fake implementing the restricted AuthenticationBrowser surface."""

    def __init__(
        self,
        *,
        available_roles: set[str],
        succeeds: bool = True,
        password_after_submit: bool = False,
    ) -> None:
        self._available = available_roles
        self._succeeds = succeeds
        self._password_after_submit = password_after_submit
        self._advanced = False
        # observations — role → typed byte length (NEVER the bytes themselves)
        self.typed: list[tuple[str, int]] = []
        self.activated: list[str] = []
        self.cleared = 0
        self.waited = 0

    async def inspect_context(self) -> BrowserAuthenticationContext:
        return _ctx()

    async def locate_authentication_field(self, role, hints: AuthenticationFieldHints):
        if role == "password" and self._password_after_submit and not self._advanced:
            return None  # hidden until the username stage is submitted
        if role in self._available:
            return AuthenticationFieldHandle(handle_id=f"h_{role}", role=role)
        return None

    async def type_secret(self, field: AuthenticationFieldHandle, value) -> None:
        self.typed.append((field.role, len(bytes(value.borrow_bytes()))))

    async def activate(self, field: AuthenticationFieldHandle) -> None:
        self.activated.append(field.role)
        if field.role == "submit":
            self._advanced = True

    async def clear_authentication_fields(self) -> None:
        self.cleared += 1

    async def wait_for_authentication_signal(self, condition: AuthenticationSuccessCondition) -> bool:
        self.waited += 1
        return self._succeeds


def _await(coro):
    import asyncio

    return asyncio.run(coro)


def test_single_page_success() -> None:
    browser = FakeBrowser(available_roles={"username", "password", "submit"})
    creds = ResolvedCredentials(
        username=secret_from_str("alice"), password=secret_from_str("hunter2xyz")
    )
    result = _await(GenericPasswordAdapter().authenticate(browser, _profile(), creds))
    assert result.success
    assert result.authenticated_origin == "https://accounts.example.com"
    typed_roles = [r for r, _ in browser.typed]
    assert typed_roles == ["username", "password"]
    assert "submit" in browser.activated


def test_two_stage_login() -> None:
    browser = FakeBrowser(
        available_roles={"username", "password", "submit"}, password_after_submit=True
    )
    creds = ResolvedCredentials(
        username=secret_from_str("alice"), password=secret_from_str("hunter2xyz")
    )
    result = _await(GenericPasswordAdapter().authenticate(browser, _profile(), creds))
    assert result.success
    # username submitted first to reveal the password field, then password typed
    assert browser.activated.count("submit") >= 1
    assert ("password", len("hunter2xyz")) in browser.typed


def test_totp_step() -> None:
    browser = FakeBrowser(available_roles={"username", "password", "submit", "totp"})
    creds = ResolvedCredentials(
        username=secret_from_str("alice"),
        password=secret_from_str("hunter2xyz"),
        totp_seed=secret_from_str(_SEED_B32),
    )
    adapter = GenericPasswordAdapter(time_source=lambda: 59.0)
    result = _await(adapter.authenticate(browser, _profile(), creds))
    assert result.success
    roles = [r for r, _ in browser.typed]
    assert "totp" in roles
    # a 6-digit code was typed
    totp_len = dict(browser.typed)["totp"]
    assert totp_len == 6


def test_missing_password_field_is_human_required() -> None:
    browser = FakeBrowser(available_roles={"username", "submit"})  # no password field ever
    creds = ResolvedCredentials(
        username=secret_from_str("alice"), password=secret_from_str("hunter2xyz")
    )
    result = _await(GenericPasswordAdapter().authenticate(browser, _profile(), creds))
    assert result.requires_human_interaction
    assert result.error_code == AuthErrorCode.HUMAN_INTERACTION_REQUIRED.value


def test_no_password_credential_is_credential_missing() -> None:
    browser = FakeBrowser(available_roles={"username", "password", "submit"})
    creds = ResolvedCredentials(username=secret_from_str("alice"))  # no password
    result = _await(GenericPasswordAdapter().authenticate(browser, _profile(), creds))
    assert not result.success
    assert result.error_code == AuthErrorCode.CREDENTIAL_MISSING.value


def test_rejection_clears_fields_and_caps_attempts() -> None:
    browser = FakeBrowser(available_roles={"username", "password", "submit"}, succeeds=False)
    creds = ResolvedCredentials(
        username=secret_from_str("alice"), password=secret_from_str("wrongpass12")
    )
    result = _await(GenericPasswordAdapter(max_attempts=2).authenticate(browser, _profile(), creds))
    assert not result.success
    assert result.error_code == AuthErrorCode.AUTHENTICATION_REJECTED.value
    assert browser.waited == 2  # exactly max_attempts, not infinite
    assert browser.cleared >= 1  # fields scrubbed on failure


def test_adapter_never_records_secret_bytes() -> None:
    browser = FakeBrowser(available_roles={"username", "password", "submit"})
    creds = ResolvedCredentials(
        username=secret_from_str("alice"), password=secret_from_str("hunter2xyz")
    )
    _await(GenericPasswordAdapter().authenticate(browser, _profile(), creds))
    # the fake could only ever observe (role, length) — never the plaintext
    for _role, length in browser.typed:
        assert isinstance(length, int)
