"""Credential Executor — resolves secrets and drives the adapter (design §7.1, §5.8, §11.3).

The Executor is the ONLY place :class:`~tabvis.authentication.models.ResolvedCredentials` is created
and destroyed (design §5.8). It runs the secret-bearing part of the flow (§7.1 steps 10–19):

1. re-read the live browser context and **atomically consume** the capability against it (§7.2);
2. resolve the username / password / TOTP-seed refs from the Secret Provider into a
   :class:`ResolvedCredentials`;
3. register a DLP canary fingerprint for each resolved secret (§11.3);
4. run the adapter against the restricted browser;
5. on any failure, clear the sensitive fields; **always** release the secret buffers.

Everything crossing back out is a redacted :class:`~tabvis.authentication.models.AuthenticationResult`
— a stable error code only, never an exception message, selector, username or site body (§5.3, §12.1).
In L0/L1 the Executor runs in-process / same-user; the real cleanup boundary is a short-lived worker
under L2 (§5.7). ``run_in_worker`` documents that boundary for the process-isolated deployment.
"""

from __future__ import annotations

from tabvis.authentication.adapters.base import AuthenticationAdapter, AuthenticationBrowser
from tabvis.authentication.capabilities import CapabilityStore
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import (
    AuthenticationRequest,
    AuthenticationResult,
    CredentialProfile,
    ResolvedCredentials,
)
from tabvis.credential_broker.secrets.base import SecretProvider, SecretProviderUnavailable
from tabvis.utils.debug import log_for_debugging


class CredentialExecutor:
    def __init__(
        self,
        *,
        provider: SecretProvider,
        capability_store: CapabilityStore,
        register_canary: bool = True,
    ) -> None:
        self._provider = provider
        self._capabilities = capability_store
        self._register_canary = register_canary

    async def execute(
        self,
        *,
        capability_id: str,
        profile: CredentialProfile,
        adapter: AuthenticationAdapter,
        browser: AuthenticationBrowser,
        request: AuthenticationRequest,
    ) -> AuthenticationResult:
        credentials: ResolvedCredentials | None = None
        consumed = False
        try:
            # 1. re-read live context and atomically consume the capability against it (§7.1 step 11).
            context = await browser.inspect_context()
            self._capabilities.consume(capability_id, context=context)
            consumed = True

            # 2. provider health, then resolve the referenced secrets (§7.1 step 12).
            if not await self._provider.health():
                raise AuthenticationError(AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE)
            credentials = await self._resolve(profile)

            # 3. register canaries so any later egress of these values fails closed (§11.3).
            if self._register_canary:
                self._register_canaries(credentials, profile)

            # 4. run the adapter (§7.1 steps 13–15).
            adapter_result = await adapter.authenticate(browser, profile, credentials)

            # 5. clear sensitive fields on any non-success (§7.1 step 17, §9.2).
            if not adapter_result.success:
                await browser.clear_authentication_fields()

            return AuthenticationResult(
                success=adapter_result.success,
                authenticated_origin=adapter_result.authenticated_origin,
                requires_human_interaction=adapter_result.requires_human_interaction,
                error_code=adapter_result.error_code,
            )
        except AuthenticationError as err:
            if not consumed:
                self._capabilities.invalidate(capability_id)
            await self._safe_clear(browser)
            return AuthenticationResult(success=False, error_code=err.code.value)
        except Exception as exc:  # noqa: BLE001 - redact everything unknown to a stable code
            log_for_debugging(f"[EXECUTOR] internal error: {type(exc).__name__}")
            if not consumed:
                self._capabilities.invalidate(capability_id)
            await self._safe_clear(browser)
            return AuthenticationResult(
                success=False, error_code=AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value
            )
        finally:
            # ALWAYS release the secret buffers, on every path (§5.8, §13.2).
            if credentials is not None:
                credentials.release()

    async def _resolve(self, profile: CredentialProfile) -> ResolvedCredentials:
        creds = ResolvedCredentials()
        try:
            if profile.username_secret_ref:
                creds.username = await self._resolve_one(profile.username_secret_ref)
            if profile.password_secret_ref:
                creds.password = await self._resolve_one(profile.password_secret_ref)
            if profile.totp_secret_ref:
                creds.totp_seed = await self._resolve_one(profile.totp_secret_ref)
        except Exception:
            creds.release()  # don't leak partially-resolved secrets on failure
            raise
        return creds

    async def _resolve_one(self, secret_ref: str):
        try:
            return await self._provider.resolve(secret_ref)
        except SecretProviderUnavailable:
            # Health already passed, so a failure here means the referenced secret is missing.
            raise AuthenticationError(AuthErrorCode.CREDENTIAL_MISSING) from None

    def _register_canaries(self, creds: ResolvedCredentials, profile: CredentialProfile) -> None:
        from tabvis.dlp import canary

        for value, kind in (
            (creds.username, "username"),
            (creds.password, "password"),
            (creds.totp_seed, "totp_seed"),
        ):
            if value is None:
                continue
            view = value.borrow_bytes()
            try:
                canary.register(bytes(view), tag=f"{kind}:{profile.id}")
            finally:
                del view

    async def _safe_clear(self, browser: AuthenticationBrowser) -> None:
        try:
            await browser.clear_authentication_fields()
        except Exception:  # noqa: BLE001 - never raise from cleanup
            pass


def run_in_worker() -> bool:
    """Whether the Executor should fork a short-lived worker per authentication (L2, design §5.7).

    Under L2 each authentication resolves and injects inside a fresh subprocess that is torn down
    afterwards, which is the primary memory-cleanup boundary (Python can't guarantee zeroing). This is
    gated on ``TABVIS_CREDENTIAL_BROKER_MODE=production``; L0/L1 run in-process.
    """
    import os

    return os.environ.get("TABVIS_CREDENTIAL_BROKER_MODE", "").strip().lower() == "production"
