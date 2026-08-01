"""Phase 6 managed-authentication runtime integration."""

from __future__ import annotations

import asyncio
import contextlib

from tabvis.agent.tools.browser_authenticate_tool import (
    BrowserAuthenticateInput,
    browser_authenticate_tool,
)
from tabvis.authentication.adapters.base import AuthenticationSuccessCondition
from tabvis.authentication.models import (
    AgentAuthenticationRequest,
    BrowserAuthenticationContext,
    CredentialProfile,
)
from tabvis.authentication.runtime import ManagedAuthenticationRuntime
from tabvis.credential_broker.secrets.memory import MemorySecretProvider
from tabvis.tool import ToolUseContext


class FakeController:
    def __init__(self) -> None:
        self.context = BrowserAuthenticationContext(
            browser_session_id="browser-1",
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
        self.typed: list[str] = []
        self.cleared = 0
        self.destroyed = False
        self.controlled = False
        self.restored: dict | None = None

    @contextlib.asynccontextmanager
    async def exclusive_control(self):
        self.controlled = True
        yield

    async def current_context(self):
        return self.context

    async def find_field(self, role, _hints):
        return role if role in {"username", "password", "submit"} else None

    async def type_bytes(self, handle_id, data):
        assert isinstance(data, bytes)
        self.typed.append(handle_id)

    async def activate(self, _handle_id):
        return None

    async def clear_fields(self):
        self.cleared += 1

    async def check_signal(self, _condition: AuthenticationSuccessCondition):
        return True

    async def storage_state(self):
        return {"cookies": [{"name": "sid", "value": "encrypted-by-vault"}]}

    async def restore_storage_state(self, state):
        self.restored = state
        self.context = self.context.model_copy(update={"navigation_generation": 2})

    async def destroy_context(self):
        self.destroyed = True


class FakeVault:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.ended: list[str] = []

    def create(self, **kwargs):
        self.created.append(kwargs)

    def end_task(self, task_id):
        self.ended.append(task_id)
        return 1


def _profile() -> CredentialProfile:
    return CredentialProfile(
        id="work",
        owner_user_id="user-1",
        allowed_origins=["https://accounts.example.com"],
        allowed_frame_origins=["https://accounts.example.com"],
        username_secret_ref="user-ref",
        password_secret_ref="pass-ref",
        authentication_adapter="generic_password_v1",
        approval_policy="never",
    )


def test_tool_to_runtime_to_broker_to_browser_and_vault() -> None:
    controller = FakeController()
    vault = FakeVault()
    profile = _profile()

    async def controller_factory(_agent_id, _browser_session_id):
        return controller

    runtime = ManagedAuthenticationRuntime.in_process(
        provider=MemorySecretProvider({"user-ref": "alice", "pass-ref": "correct horse"}),
        profile_lookup=lambda profile_id, user_id: (
            profile if profile_id == profile.id and user_id == profile.owner_user_id else None
        ),
        controller_factory=controller_factory,
        session_vault=vault,
    )
    context = ToolUseContext(
        agent_id="agent-1",
        principal_id="user-1",
        session_id="session-1",
        run_id="run-1",
        browser_session_id="browser-1",
        authentication_service=runtime,
    )

    async def scenario():
        return await browser_authenticate_tool.call(
            BrowserAuthenticateInput(credential_profile_id="work"),
            context,
            None,
            None,
        )

    result = asyncio.run(scenario())
    assert result.data == {
        "success": True,
        "authenticated_origin": "https://accounts.example.com",
        "requires_human_interaction": False,
        "error_code": None,
    }
    assert controller.controlled
    assert set(controller.typed) == {"username", "password"}
    assert controller.cleared >= 1
    assert len(vault.created) == 1
    assert vault.created[0]["task_id"] == "run-1"


def test_runtime_rejects_missing_trusted_context() -> None:
    class NeverClient:
        async def authenticate(self, _request):
            raise AssertionError("Broker must not be called")

    runtime = ManagedAuthenticationRuntime(NeverClient())
    result = asyncio.run(
        runtime.authenticate(
            AgentAuthenticationRequest(credential_profile_id="work"),
            context=ToolUseContext(agent_id="agent-1"),
        )
    )
    assert not result.success
    assert result.error_code == "internal_authentication_error"


def test_runtime_restores_vault_session_before_resolving_credentials() -> None:
    controller = FakeController()
    profile = _profile().model_copy(update={"session_validation_cookie_name": "sid"})

    class RestoreVault(FakeVault):
        def find_for_profile(self, **_kwargs):
            return "authsess-1", {"cookies": [{"name": "sid", "value": "opaque"}]}

        def delete(self, _session_id):
            raise AssertionError("valid restored session must not be deleted")

    vault = RestoreVault()

    async def controller_factory(_agent_id, _browser_session_id):
        return controller

    runtime = ManagedAuthenticationRuntime.in_process(
        provider=MemorySecretProvider(healthy=False),
        profile_lookup=lambda _profile_id, _user_id: profile,
        controller_factory=controller_factory,
        session_vault=vault,
    )
    context = ToolUseContext(
        agent_id="agent-1",
        principal_id="user-1",
        run_id="run-1",
        browser_session_id="browser-1",
    )
    result = asyncio.run(
        runtime.authenticate(
            AgentAuthenticationRequest(credential_profile_id="work"),
            context=context,
        )
    )
    assert result.success
    assert controller.restored is not None
    assert controller.typed == []
    assert vault.created == []  # do not duplicate an existing restored session


def test_runtime_cancellation_cancels_broker_call() -> None:
    class SlowClient:
        cancelled = False

        async def authenticate(self, _request):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    client = SlowClient()
    runtime = ManagedAuthenticationRuntime(client, timeout_seconds=30)
    context = ToolUseContext(
        agent_id="agent-1",
        principal_id="user-1",
        run_id="run-1",
        browser_session_id="browser-1",
    )

    async def scenario():
        task = asyncio.create_task(
            runtime.authenticate(
                AgentAuthenticationRequest(credential_profile_id="work"),
                context=context,
            )
        )
        await asyncio.sleep(0)
        context.abort_controller.abort()
        return await task

    result = asyncio.run(scenario())
    assert result.error_code == "authentication_timeout"
    assert client.cancelled
