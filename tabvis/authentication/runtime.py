"""Managed-authentication composition root (Phase 6).

This is the only runtime layer that joins the otherwise isolated authentication components:

``BrowserAuthenticateTool -> ManagedAuthenticationRuntime -> BrokerClient -> CredentialBroker``

The tool supplies only an ``AgentAuthenticationRequest``.  Trusted run identity is taken from
``ToolUseContext`` and enriched here; local development additionally binds the live Playwright page
behind an exclusive Browser Host lease.  Production requires an external Broker endpoint and never
silently falls back to the in-process L0 composition.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import socket
import stat
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from tabvis.authentication.broker_client import (
    BrokerClient,
    InProcessBrokerClient,
    SocketBrokerClient,
    enrich_request,
)
from tabvis.authentication.adapters.base import AuthenticationSuccessCondition
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import AgentAuthenticationRequest, AuthenticationResult
from tabvis.authentication.profile_store import get_for_user
from tabvis.browser.host import begin_authentication
from tabvis.credential_broker.broker import CredentialBroker, new_request_id
from tabvis.credential_broker.secrets.keychain import NativeKeychainProvider
from tabvis.dlp.gateway import get_dlp_gateway
from tabvis.utils.debug import log_for_debugging

ControllerFactory = Callable[[str, str], Awaitable[Any]]
_approval_handler: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "tabvis_managed_auth_approval_handler", default=None
)
_logger = logging.getLogger("tabvis.authentication.audit")


class ManagedAuthenticationConfigurationError(RuntimeError):
    """The enabled managed-authentication runtime cannot be composed safely."""


class ManagedAuthenticationRuntime:
    """Lifecycle owner and trusted request enricher for one Broker client."""

    def __init__(
        self,
        client: BrokerClient,
        *,
        controller_factory: ControllerFactory | None = None,
        local_broker: CredentialBroker | None = None,
        timeout_seconds: float = 120.0,
        session_vault: Any = None,
        profile_lookup: Any = get_for_user,
    ) -> None:
        self._client = client
        self._controller_factory = controller_factory
        self._local_broker = local_broker
        self._timeout_seconds = max(1.0, timeout_seconds)
        self._session_vault = session_vault
        self._profile_lookup = profile_lookup
        self._browsers: dict[str, Any] = {}
        self._controllers: dict[str, Any] = {}
        self._auth_sessions: dict[str, Any] = {}
        self._active_profiles: dict[str, str] = {}
        self._restored_requests: set[str] = set()
        self._active_operations: set[asyncio.Task[Any]] = set()
        self._closed = False
        if self._session_vault is not None:
            with contextlib.suppress(Exception):
                self._session_vault.purge_expired()

    @classmethod
    def in_process(
        cls,
        *,
        provider: Any,
        profile_lookup: Any,
        controller_factory: ControllerFactory,
        approval_callback: Any = None,
        audit_sink: Any = None,
        capability_ttl_seconds: int = 30,
        timeout_seconds: float = 120.0,
        session_vault: Any = None,
    ) -> "ManagedAuthenticationRuntime":
        """Compose the L0 Broker/Browser Host chain (development and integration tests)."""
        holder: dict[str, ManagedAuthenticationRuntime] = {}
        broker = CredentialBroker(
            provider=provider,
            profile_lookup=profile_lookup,
            browser_provider=lambda session_id: holder["runtime"].browser_for_session(session_id),
            approval_callback=approval_callback,
            audit_sink=audit_sink,
            session_restorer=lambda request, profile, browser: holder[
                "runtime"
            ]._restore_session(request, profile, browser),
            capability_ttl_seconds=capability_ttl_seconds,
        )
        runtime = cls(
            InProcessBrokerClient(broker),
            controller_factory=controller_factory,
            local_broker=broker,
            timeout_seconds=timeout_seconds,
            session_vault=session_vault,
            profile_lookup=profile_lookup,
        )
        holder["runtime"] = runtime
        return runtime

    @property
    def healthy(self) -> bool:
        return not self._closed

    async def close(self) -> None:
        """Stop accepting work, cancel in-flight authentication, and drop transient registrations."""
        self._closed = True
        operations = [task for task in self._active_operations if not task.done()]
        for task in operations:
            task.cancel()
        if operations:
            await asyncio.gather(*operations, return_exceptions=True)
        self._browsers.clear()
        self._controllers.clear()
        self._auth_sessions.clear()
        self._active_profiles.clear()

    async def authenticate(
        self,
        agent_request: AgentAuthenticationRequest,
        *,
        context: Any,
    ) -> AuthenticationResult:
        """Enrich and execute one authentication using only trusted context fields."""
        if self._closed:
            return _failure(AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR)

        agent_id = _trusted_id(context, "agent_id")
        user_id = _trusted_id(context, "principal_id")
        task_id = _trusted_id(context, "run_id")
        browser_session_id = _trusted_id(context, "browser_session_id")
        if not all((agent_id, user_id, task_id, browser_session_id)):
            return _failure(AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR)

        request = enrich_request(
            agent_request,
            request_id=new_request_id(),
            browser_session_id=browser_session_id,
            task_id=task_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        signal = getattr(getattr(context, "abort_controller", None), "signal", None)
        approval_token = _approval_handler.set(getattr(context, "handle_elicitation", None))
        try:
            if self._controller_factory is None:
                return await self._bounded_call(self._client.authenticate(request), signal)
            return await self._bounded_call(
                self._authenticate_on_local_browser(request, agent_id, browser_session_id),
                signal,
            )
        finally:
            _approval_handler.reset(approval_token)

    async def end_task(self, task_id: str) -> int:
        """Clean non-reusable authenticated sessions created by a terminal task."""
        if self._session_vault is None:
            return 0
        return await asyncio.to_thread(self._session_vault.end_task, task_id)

    def revoke_profile(self, credential_profile_id: str) -> int:
        """Cascade-delete Vault sessions after profile disable/delete/revocation."""
        if self._session_vault is None:
            return 0
        return self._session_vault.revoke_for_profile(credential_profile_id)

    async def _authenticate_on_local_browser(
        self, request: Any, agent_id: str, browser_session_id: str
    ) -> AuthenticationResult:
        controller = await self._controller_factory(agent_id, browser_session_id)
        control = (
            controller.exclusive_control()
            if hasattr(controller, "exclusive_control")
            else _no_op_control()
        )
        async with control:
            session = None
            result: AuthenticationResult | None = None
            try:
                async with begin_authentication(
                    controller,
                    browser_session_id=browser_session_id,
                    task_id=request.task_id,
                    request_id=request.request_id,
                ) as session:
                    self._browsers[browser_session_id] = session.browser
                    self._controllers[browser_session_id] = controller
                    self._auth_sessions[browser_session_id] = session
                    self._active_profiles[browser_session_id] = request.credential_profile_id
                    result = await self._client.authenticate(request)
                    if result.success:
                        if hasattr(controller, "arm_post_auth_redaction"):
                            controller.arm_post_auth_redaction()
                    elif result.error_code in {
                        AuthErrorCode.DLP_BLOCKED.value,
                        AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value,
                    }:
                        session.must_destroy_context = True
                # ``begin_authentication.__aexit__`` has now confirmed sensitive fields were cleared.
                # Only then may storage_state leave the Browser Host for encrypted Vault persistence.
                if result.success and not session.must_destroy_context:
                    await self._persist_session(controller, request)
                return result
            finally:
                self._browsers.pop(browser_session_id, None)
                self._controllers.pop(browser_session_id, None)
                self._auth_sessions.pop(browser_session_id, None)
                self._active_profiles.pop(browser_session_id, None)
                if session is not None and session.must_destroy_context:
                    with contextlib.suppress(Exception):
                        await controller.destroy_context()

    async def _persist_session(self, controller: Any, request: Any) -> None:
        """Persist successful storage state when a secure Session Vault is configured."""
        if self._session_vault is None or not hasattr(controller, "storage_state"):
            return
        if request.request_id in self._restored_requests:
            self._restored_requests.discard(request.request_id)
            return
        profile = self._profile_lookup(request.credential_profile_id, request.user_id)
        if profile is None:
            return
        try:
            state = await controller.storage_state()
            await asyncio.to_thread(
                self._session_vault.create,
                storage_state=state,
                user_id=request.user_id,
                task_id=request.task_id,
                credential_profile_id=profile.id,
                allowed_origins=profile.allowed_origins,
                ttl_seconds=profile.session_ttl_seconds,
                reusable_across_tasks=profile.reusable_across_tasks,
            )
        except Exception:  # noqa: BLE001 - auth succeeded; Vault persistence fails closed to no reuse
            log_for_debugging("[AUTH] Session Vault persistence failed")

    async def _restore_session(
        self, request: Any, profile: Any, browser: Any
    ) -> AuthenticationResult | None:
        """Restore an encrypted session and validate it with an explicit strong cookie signal."""
        if (
            self._session_vault is None
            or not profile.session_validation_cookie_name
        ):
            return None
        controller = self._controllers.get(request.browser_session_id)
        if controller is None or not hasattr(controller, "restore_storage_state"):
            return None
        found = await asyncio.to_thread(
            self._session_vault.find_for_profile,
            credential_profile_id=profile.id,
            user_id=request.user_id,
            task_id=request.task_id,
            requested_origins=profile.allowed_origins,
        )
        if found is None:
            return None
        session_id, storage_state = found
        try:
            await controller.restore_storage_state(storage_state)
            valid = await browser.wait_for_authentication_signal(
                AuthenticationSuccessCondition(
                    kind="cookie_present",
                    cookie_name=profile.session_validation_cookie_name,
                    timeout_seconds=min(5.0, self._timeout_seconds),
                )
            )
            if not valid:
                await asyncio.to_thread(self._session_vault.delete, session_id)
                return _failure(AuthErrorCode.PAGE_CHANGED)
            live = await browser.inspect_context()
            if live.top_level_origin not in profile.allowed_origins:
                await asyncio.to_thread(self._session_vault.delete, session_id)
                return _failure(AuthErrorCode.ORIGIN_NOT_ALLOWED)
            self._restored_requests.add(request.request_id)
            return AuthenticationResult(
                success=True,
                authenticated_origin=live.top_level_origin,
            )
        except Exception:  # noqa: BLE001 - invalidate corrupt/stale state; caller may retry fresh
            await asyncio.to_thread(self._session_vault.delete, session_id)
            return _failure(AuthErrorCode.PAGE_CHANGED)

    async def _bounded_call(self, operation: Awaitable[AuthenticationResult], signal: Any) -> AuthenticationResult:
        task = asyncio.create_task(operation)
        self._active_operations.add(task)
        abort_task: asyncio.Task[Any] | None = None
        try:
            waiters: set[asyncio.Task[Any]] = {task}
            if signal is not None and hasattr(signal, "wait"):
                if getattr(signal, "aborted", False):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    self._invalidate_capabilities()
                    return _failure(AuthErrorCode.AUTHENTICATION_TIMEOUT)
                abort_task = asyncio.create_task(signal.wait())
                waiters.add(abort_task)
            done, _pending = await asyncio.wait(
                waiters,
                timeout=self._timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._invalidate_capabilities()
            return _failure(AuthErrorCode.AUTHENTICATION_TIMEOUT)
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._invalidate_capabilities()
            raise
        except Exception:  # noqa: BLE001 - never expose transport/browser exception details
            return _failure(AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR)
        finally:
            self._active_operations.discard(task)
            if abort_task is not None:
                abort_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await abort_task

    def _invalidate_capabilities(self) -> None:
        if self._local_broker is not None:
            self._local_broker.invalidate_all_capabilities()

    def browser_for_session(self, browser_session_id: str) -> Any:
        """Broker-side provider for the transient restricted browser registration."""
        return self._browsers.get(browser_session_id)

    def handle_dlp_block(self, event: Any) -> None:
        """Fail closed for every in-flight authentication after a canary/forbidden-object hit."""
        if self._local_broker is not None:
            self._local_broker.invalidate_all_capabilities()
        for session in list(self._auth_sessions.values()):
            session.must_destroy_context = True
        if self._session_vault is not None:
            for profile_id in set(self._active_profiles.values()):
                with contextlib.suppress(Exception):
                    self._session_vault.revoke_for_profile(profile_id)
        # The DLP event contains only a fingerprint and safe metadata.
        with contextlib.suppress(Exception):
            _logger.error("dlp.secret_blocked %s", event.model_dump_json())


async def _default_controller_factory(agent_id: str, browser_session_id: str) -> Any:
    from tabvis.browser.manager import bind_agent, get_or_create_browser_service, unbind_agent
    from tabvis.browser.playwright_auth import PlaywrightPageController

    token = bind_agent(agent_id)
    try:
        service = await get_or_create_browser_service()
    finally:
        unbind_agent(token)
    return PlaywrightPageController(service, browser_session_id=browser_session_id)


async def _request_approval(request: Any, origin: str) -> bool:
    """Ask through the trusted Orchestrator callback; headless/no-callback defaults to deny."""
    handler = _approval_handler.get()
    if handler is None:
        return False
    payload = {
        "type": "managed_authentication_approval",
        "credential_profile_id": request.credential_profile_id,
        "origin": origin,
        "request_id": request.request_id,
    }
    try:
        response = await handler(payload)
    except Exception:  # noqa: BLE001
        return False
    if isinstance(response, bool):
        return response
    if isinstance(response, dict):
        return bool(response.get("approved") or response.get("allow"))
    return False


def _audit_sink(event: dict) -> None:
    decision = get_dlp_gateway().scrub("audit", event)
    if decision.blocked:
        raise RuntimeError("authentication audit blocked by DLP")
    _logger.info("credential_profile_used %s", json.dumps(decision.payload, sort_keys=True))


def _build_runtime() -> ManagedAuthenticationRuntime:
    from tabvis.browser import secret_store

    secret_store.assert_production_backend()
    endpoint = os.environ.get("TABVIS_CREDENTIAL_BROKER_ENDPOINT", "").strip()
    mode = os.environ.get("TABVIS_CREDENTIAL_BROKER_MODE", "development").strip().lower()
    timeout = _float_env("TABVIS_AUTHENTICATION_TIMEOUT_SECONDS", 120.0)
    if mode not in {"development", "ipc", "production"}:
        raise ManagedAuthenticationConfigurationError(
            "TABVIS_CREDENTIAL_BROKER_MODE must be development, ipc, or production"
        )
    if mode == "production":
        from tabvis.utils.env_utils import is_env_truthy

        if not is_env_truthy(os.environ.get("TABVIS_MANAGED_AUTH_L2_VERIFIED")):
            raise ManagedAuthenticationConfigurationError(
                "production managed authentication requires TABVIS_MANAGED_AUTH_L2_VERIFIED=1"
            )

    if endpoint:
        _validate_broker_endpoint(endpoint)
        return ManagedAuthenticationRuntime(
            SocketBrokerClient(endpoint),
            timeout_seconds=timeout,
        )
    if mode in {"ipc", "production"}:
        raise ManagedAuthenticationConfigurationError(
            f"{mode} managed authentication requires TABVIS_CREDENTIAL_BROKER_ENDPOINT"
        )

    provider = NativeKeychainProvider()
    runtime = ManagedAuthenticationRuntime.in_process(
        provider=provider,
        profile_lookup=get_for_user,
        controller_factory=_default_controller_factory,
        approval_callback=_request_approval,
        audit_sink=_audit_sink,
        capability_ttl_seconds=min(30, max(1, _int_env("TABVIS_AUTH_CAPABILITY_TTL_SECONDS", 30))),
        timeout_seconds=timeout,
        session_vault=_build_default_session_vault(),
    )
    from tabvis.dlp.gateway import DLPGateway, set_dlp_gateway

    set_dlp_gateway(DLPGateway(on_secret_blocked=runtime.handle_dlp_block))
    return runtime


def _build_default_session_vault() -> Any:
    """Create a Vault only when its KEK can live in a secure OS backend."""
    from tabvis.browser import secret_store

    if not secret_store.has_secure_backend():
        return None
    try:
        import base64

        from tabvis.session_vault import LocalKeyProvider, SessionVault
        from tabvis.utils.env_utils import get_tabvis_config_home_dir

        path = os.path.join(get_tabvis_config_home_dir(), "auth-session-vault-key.json")
        ref: str | None = None
        with contextlib.suppress(OSError, ValueError, KeyError):
            with open(path, encoding="utf-8") as fh:
                ref = str(json.load(fh)["secret_ref"])
        encoded = secret_store.get(ref)
        if encoded is None:
            encoded = base64.b64encode(os.urandom(32)).decode("ascii")
            ref = secret_store.put(encoded)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"secret_ref": ref}, fh)
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        key = base64.b64decode(encoded)
        del encoded
        return SessionVault(LocalKeyProvider(key, key_id=ref or "managed-auth-vault"))
    except Exception:  # noqa: BLE001 - no plaintext fallback; simply disable persisted sessions
        return None


def _validate_broker_endpoint(endpoint: str) -> None:
    """Validate the configured Unix socket before Gateway/CLI advertises auth readiness."""
    if not os.path.isabs(endpoint):
        raise ManagedAuthenticationConfigurationError(
            "TABVIS_CREDENTIAL_BROKER_ENDPOINT must be an absolute Unix socket path"
        )
    # sockaddr_un is commonly 104 bytes on macOS and 108 on Linux. Keep a portable margin.
    if len(os.fsencode(endpoint)) > 100:
        raise ManagedAuthenticationConfigurationError(
            "TABVIS_CREDENTIAL_BROKER_ENDPOINT exceeds the portable Unix socket path limit"
        )
    try:
        info = os.stat(endpoint)
    except OSError as exc:
        raise ManagedAuthenticationConfigurationError(
            "configured Credential Broker socket is unavailable"
        ) from exc
    if not stat.S_ISSOCK(info.st_mode):
        raise ManagedAuthenticationConfigurationError(
            "TABVIS_CREDENTIAL_BROKER_ENDPOINT is not a Unix socket"
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ManagedAuthenticationConfigurationError(
            "configured Credential Broker socket is not owned by the current user"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ManagedAuthenticationConfigurationError(
            "Credential Broker socket permissions must be owner-only (0600)"
        )
    if not hasattr(socket, "AF_UNIX"):
        raise ManagedAuthenticationConfigurationError(
            "this platform does not support Unix-domain Credential Broker IPC"
        )


@contextlib.asynccontextmanager
async def _no_op_control():
    yield


def _trusted_id(context: Any, name: str) -> str:
    value = getattr(context, name, None)
    return value.strip() if isinstance(value, str) else ""


def _failure(code: AuthErrorCode) -> AuthenticationResult:
    return AuthenticationResult(success=False, error_code=code.value)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


_runtime_lock = threading.RLock()
_runtime: ManagedAuthenticationRuntime | None = None


def get_managed_authentication_runtime() -> ManagedAuthenticationRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None or not _runtime.healthy:
            _runtime = _build_runtime()
            from tabvis.utils.cleanup_registry import register_cleanup

            register_cleanup(_runtime.close)
        return _runtime


def set_managed_authentication_runtime(runtime: ManagedAuthenticationRuntime | None) -> None:
    """Test/deployment override for the process-global composition root."""
    global _runtime
    with _runtime_lock:
        _runtime = runtime


def revoke_profile_sessions(credential_profile_id: str) -> int:
    """Revoke sessions without constructing a new runtime as a side effect."""
    with _runtime_lock:
        runtime = _runtime
    if runtime is None or not runtime.healthy:
        return 0
    return runtime.revoke_profile(credential_profile_id)
