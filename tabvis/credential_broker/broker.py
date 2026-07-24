"""Credential Broker orchestration (design §6.2, §7.1, §8).

The Broker is the trusted-domain entry point. Given an enriched :class:`AuthenticationRequest` it runs
the non-secret control flow (§7.1 steps 5–9, 16, 18–20) and hands the secret-bearing part to the
:class:`~tabvis.credential_broker.executor.CredentialExecutor`:

1. load the profile (ownership re-checked) — else ``profile_not_found``;
2. read the live browser context from the Browser Host;
3. run the composed policy check (§8.4);
4. obtain approval per policy (§8.5);
5. issue a one-time capability bound to the live context (§5.6);
6. resolve + inject via the Executor;
7. emit a whitelist audit event (§12.1) and return a redacted result.

All dependencies are injected so the Broker is testable in-process (L0) and identical when driven over
IPC by :mod:`tabvis.credential_broker.server` (L1/L2). The Broker never returns a capability, secret or
exception text to its caller (§4.1, §5.6).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Awaitable, Callable

from tabvis.authentication.adapters.registry import get_adapter, is_registered_adapter
from tabvis.authentication.approval import ApprovalService
from tabvis.authentication.audit import build_credential_used_event
from tabvis.authentication.capabilities import (
    DEFAULT_CAPABILITY_TTL_SECONDS,
    CapabilityStore,
)
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import (
    AuthenticationRequest,
    AuthenticationResult,
    BrowserAuthenticationContext,
    CredentialProfile,
)
from tabvis.authentication.policy_engine import check_authorization
from tabvis.credential_broker.executor import CredentialExecutor
from tabvis.credential_broker.secrets.base import SecretProvider

# Injected collaborators the Broker needs from the surrounding runtime.
ProfileLookup = Callable[[str, str], CredentialProfile | None]  # (profile_id, user_id) -> profile
BrowserProvider = Callable[[str], "object"]  # browser_session_id -> AuthenticationBrowser
# (request, origin) -> approved?  Prompts the user through a trusted UI (design §9.5, §18.7).
ApprovalCallback = Callable[[AuthenticationRequest, str], Awaitable[bool]]
AuditSink = Callable[[dict], None]


async def _auto_deny(_request: AuthenticationRequest, _origin: str) -> bool:
    """Default approval callback: deny when the runtime wires no trusted approval UI (fail closed)."""
    return False


class CredentialBroker:
    def __init__(
        self,
        *,
        provider: SecretProvider,
        profile_lookup: ProfileLookup,
        browser_provider: BrowserProvider,
        approval_service: ApprovalService | None = None,
        approval_callback: ApprovalCallback | None = None,
        capability_store: CapabilityStore | None = None,
        audit_sink: AuditSink | None = None,
        capability_ttl_seconds: int = DEFAULT_CAPABILITY_TTL_SECONDS,
    ) -> None:
        self._provider = provider
        self._profile_lookup = profile_lookup
        self._browser_provider = browser_provider
        self._approval = approval_service or ApprovalService()
        self._approval_callback = approval_callback or _auto_deny
        self._capabilities = capability_store or CapabilityStore()
        self._audit_sink = audit_sink
        self._capability_ttl = capability_ttl_seconds
        self._executor = CredentialExecutor(
            provider=provider, capability_store=self._capabilities
        )
        self._lock = threading.RLock()
        self._uses: dict[str, int] = {}
        # A single active lease at a time across the Broker (design §8.4 "没有另一个认证租约").
        self._active_session: str | None = None

    async def authenticate(self, request: AuthenticationRequest) -> AuthenticationResult:
        adapter_name = "unknown"
        origin: str | None = None
        try:
            profile = self._profile_lookup(request.credential_profile_id, request.user_id)
            if profile is None:
                return self._deny(request, adapter_name, origin, AuthErrorCode.PROFILE_NOT_FOUND)
            adapter_name = profile.authentication_adapter
            if not is_registered_adapter(adapter_name):
                return self._deny(
                    request, adapter_name, origin, AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR
                )

            browser = self._browser_provider(request.browser_session_id)
            if browser is None:
                return self._deny(request, adapter_name, origin, AuthErrorCode.BROWSER_LOCKED)
            context: BrowserAuthenticationContext = await browser.inspect_context()
            origin = context.top_level_origin

            # -- lease exclusivity (§13.1): only one authentication at a time ---------------------
            with self._lock:
                another_lease = (
                    self._active_session is not None
                    and self._active_session != request.browser_session_id
                )
            if another_lease:
                return self._deny(request, adapter_name, origin, AuthErrorCode.BROWSER_LOCKED)

            # -- policy (§8.4) -------------------------------------------------------------------
            code = check_authorization(
                profile=profile,
                context=context,
                requesting_user_id=request.user_id,
                uses_so_far=self._uses.get(profile.id, 0),
                another_lease_held=False,
                secret_provider_healthy=await self._provider.health(),
                audit_available=self._audit_available(),
            )
            if code is not None:
                return self._deny(request, adapter_name, origin, code)

            # -- approval (§8.5) -----------------------------------------------------------------
            approved_by: str | None = None
            if self._approval.requires_approval(
                policy=profile.approval_policy,
                user_id=request.user_id,
                credential_profile_id=profile.id,
                origin=origin,
            ):
                granted = await self._approval_callback(request, origin)
                if not granted:
                    return self._deny(request, adapter_name, origin, AuthErrorCode.APPROVAL_DENIED)
                self._approval.record_approval(
                    user_id=request.user_id,
                    credential_profile_id=profile.id,
                    origin=origin,
                    approved_by=request.user_id,
                )
                approved_by = request.user_id
            else:
                approved_by = None

            # -- capability + execution (§5.6, §7.1) ---------------------------------------------
            with self._lock:
                self._active_session = request.browser_session_id
            try:
                capability = self._capabilities.issue(
                    credential_profile_id=profile.id,
                    context=context,
                    task_id=request.task_id,
                    user_id=request.user_id,
                    ttl_seconds=self._capability_ttl,
                )
                adapter = get_adapter(adapter_name)
                result = await self._executor.execute(
                    capability_id=capability.id,
                    profile=profile,
                    adapter=adapter,
                    browser=browser,
                    request=request,
                )
            finally:
                with self._lock:
                    self._active_session = None

            if result.success:
                self._uses[profile.id] = self._uses.get(profile.id, 0) + 1
            self._emit_audit(
                request, adapter_name, result.authenticated_origin or origin, result, approved_by
            )
            return result
        except Exception:  # noqa: BLE001 - the Broker never leaks an exception outward (§12.1)
            with self._lock:
                self._active_session = None
            return self._deny(request, adapter_name, origin, AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR)

    # ------------------------------------------------------------------------------------------

    def _audit_available(self) -> bool:
        """Audit is 'available' if a sink is wired, or if fail-closed on audit isn't required (§8.4)."""
        import os

        from tabvis.utils.env_utils import is_env_truthy

        if self._audit_sink is not None:
            return True
        # No sink: only block when the deployment demands audit fail-closed (§17).
        return not is_env_truthy(os.environ.get("TABVIS_AUTH_AUDIT_FAIL_CLOSED"))

    def _deny(
        self,
        request: AuthenticationRequest,
        adapter: str,
        origin: str | None,
        code: AuthErrorCode,
    ) -> AuthenticationResult:
        result = AuthenticationResult(success=False, error_code=code.value)
        self._emit_audit(request, adapter, origin, result, approved_by=None)
        return result

    def _emit_audit(
        self,
        request: AuthenticationRequest,
        adapter: str,
        origin: str | None,
        result: AuthenticationResult,
        approved_by: str | None,
    ) -> None:
        if self._audit_sink is None:
            return
        event = build_credential_used_event(
            request_id=request.request_id,
            credential_profile_id=request.credential_profile_id,
            origin=origin,
            task_id=request.task_id,
            user_id=request.user_id,
            approved_by=approved_by,
            adapter=adapter,
            success=result.success,
            error_code=result.error_code,
        )
        try:
            self._audit_sink(event.model_dump())
        except Exception:  # noqa: BLE001 - audit failure must not leak or crash the flow
            pass


def new_request_id() -> str:
    return "authreq_" + uuid.uuid4().hex[:16]
