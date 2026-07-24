"""Generic username/password (+ optional TOTP) adapter (design §9.2).

Handles the common shapes (§9.2): single-page username+password, two-stage (username → password),
and an optional TOTP step. It drives only the restricted :class:`AuthenticationBrowser`, so by
construction it cannot read a field value, cookie or DOM. MUST-rules enforced here (§9.2):

* locate fields by **semantic** role/hints, never a model selector;
* never return an existing field value to the caller;
* clear username / password / TOTP fields on failure;
* cap attempts and return a **structured** error rather than looping forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from tabvis.authentication.adapters.base import (
    AdapterAuthenticationResult,
    AuthenticationBrowser,
    AuthenticationFieldHints,
    AuthenticationSuccessCondition,
)
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import CredentialProfile, ResolvedCredentials
from tabvis.authentication import totp as totp_mod

_USERNAME_HINTS = AuthenticationFieldHints(
    autocomplete="username",
    input_type="text",
    label_contains=["user", "email", "account", "login"],
    name_contains=["user", "email", "login"],
)
_PASSWORD_HINTS = AuthenticationFieldHints(
    autocomplete="current-password",
    input_type="password",
    label_contains=["password", "passcode"],
    name_contains=["pass", "pwd"],
)
_TOTP_HINTS = AuthenticationFieldHints(
    autocomplete="one-time-code",
    input_type="text",
    label_contains=["code", "otp", "authenticator", "verification"],
    name_contains=["otp", "code", "totp"],
)


class GenericPasswordAdapter:
    """Config-light adapter for standard password logins (design §9.2)."""

    name = "generic_password_v1"

    def __init__(
        self,
        *,
        max_attempts: int = 2,
        time_source: Callable[[], float] = time.time,
        success_condition: AuthenticationSuccessCondition | None = None,
    ) -> None:
        # attempt cap (§9.2 "达到尝试上限后返回结构化错误，禁止无限重试" / §17 TABVIS_AUTH_MAX_ATTEMPTS).
        self._max_attempts = max(1, max_attempts)
        # trusted time source for TOTP (§9.3); injectable for tests.
        self._time_source = time_source
        # default strong signal: the logged-in UI appears / login form disappears (§9.4). A site
        # adapter would override this with a cookie/account-API condition.
        self._success_condition = success_condition or AuthenticationSuccessCondition(
            kind="logged_in_ui"
        )

    async def authenticate(
        self,
        browser: AuthenticationBrowser,
        profile: CredentialProfile,
        credentials: ResolvedCredentials,
    ) -> AdapterAuthenticationResult:
        if credentials.password is None:
            return AdapterAuthenticationResult(
                success=False, error_code=AuthErrorCode.CREDENTIAL_MISSING.value
            )

        last_error = AuthErrorCode.AUTHENTICATION_REJECTED
        for _attempt in range(self._max_attempts):
            outcome = await self._one_attempt(browser, credentials)
            if outcome.success or outcome.requires_human_interaction:
                return outcome
            last_error = AuthErrorCode(outcome.error_code) if outcome.error_code else last_error
            # A structural problem won't fix itself on retry — stop and surface it.
            if last_error in (
                AuthErrorCode.HUMAN_INTERACTION_REQUIRED,
                AuthErrorCode.CREDENTIAL_MISSING,
            ):
                break
            await browser.clear_authentication_fields()

        return AdapterAuthenticationResult(success=False, error_code=last_error.value)

    async def _one_attempt(
        self, browser: AuthenticationBrowser, credentials: ResolvedCredentials
    ) -> AdapterAuthenticationResult:
        # -- username (single-page or first stage) ---------------------------------------------
        username_field = await browser.locate_authentication_field("username", _USERNAME_HINTS)
        if credentials.username is not None and username_field is not None:
            await browser.type_secret(username_field, credentials.username)

        # -- password (may require advancing a two-stage form) ---------------------------------
        password_field = await browser.locate_authentication_field("password", _PASSWORD_HINTS)
        if password_field is None:
            # two-stage: submit the username to reveal the password step, then re-locate.
            submit = await browser.locate_authentication_field("submit", AuthenticationFieldHints())
            if submit is not None:
                await browser.activate(submit)
            password_field = await browser.locate_authentication_field("password", _PASSWORD_HINTS)
        if password_field is None:
            # Unknown / unsupported page structure — hand off to a human (§9.5).
            return AdapterAuthenticationResult(
                success=False,
                requires_human_interaction=True,
                error_code=AuthErrorCode.HUMAN_INTERACTION_REQUIRED.value,
            )
        assert credentials.password is not None  # guarded by caller
        await browser.type_secret(password_field, credentials.password)

        # -- submit the password ---------------------------------------------------------------
        submit = await browser.locate_authentication_field("submit", AuthenticationFieldHints())
        if submit is not None:
            await browser.activate(submit)

        # -- optional TOTP (usually a step after the password) ---------------------------------
        if credentials.totp_seed is not None:
            totp_field = await browser.locate_authentication_field("totp", _TOTP_HINTS)
            if totp_field is not None:
                code = totp_mod.generate_totp(credentials.totp_seed, at=self._time_source())
                try:
                    await browser.type_secret(totp_field, code)
                finally:
                    code.release()  # the generated code is a secret — scrub immediately (§9.3)
                submit2 = await browser.locate_authentication_field(
                    "submit", AuthenticationFieldHints()
                )
                if submit2 is not None:
                    await browser.activate(submit2)

        # -- wait for a strong success signal (never URL-change alone, §9.4) -------------------
        signalled = await browser.wait_for_authentication_signal(self._success_condition)
        if not signalled:
            return AdapterAuthenticationResult(
                success=False, error_code=AuthErrorCode.AUTHENTICATION_REJECTED.value
            )

        # authenticated_origin is recomputed by the host from the live page (§9.4), not asserted here.
        ctx = await browser.inspect_context()
        return AdapterAuthenticationResult(success=True, authenticated_origin=ctx.top_level_origin)
