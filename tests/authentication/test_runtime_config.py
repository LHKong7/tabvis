"""Managed-authentication composition and production fail-closed gates."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile

import pytest

from tabvis.authentication import runtime
from tabvis.browser import secret_store
from tabvis.ui.cli.print import _build_tool_use_context


def test_endpoint_validation_accepts_owner_only_unix_socket() -> None:
    directory = tempfile.mkdtemp(prefix="tva-", dir="/tmp")
    path = os.path.join(directory, "broker.sock")

    async def scenario():
        server = await asyncio.start_unix_server(lambda _r, _w: None, path=path)
        os.chmod(path, 0o600)
        try:
            runtime._validate_broker_endpoint(path)
        finally:
            server.close()
            await server.wait_closed()
            with contextlib.suppress(OSError):
                os.unlink(path)
            os.rmdir(directory)

    asyncio.run(scenario())


def test_endpoint_validation_rejects_relative_path() -> None:
    with pytest.raises(runtime.ManagedAuthenticationConfigurationError):
        runtime._validate_broker_endpoint("broker.sock")


def test_production_requires_explicit_l2_verification(monkeypatch) -> None:
    monkeypatch.setattr(secret_store, "assert_production_backend", lambda: None)
    monkeypatch.setenv("TABVIS_CREDENTIAL_BROKER_MODE", "production")
    monkeypatch.delenv("TABVIS_MANAGED_AUTH_L2_VERIFIED", raising=False)
    with pytest.raises(runtime.ManagedAuthenticationConfigurationError, match="L2_VERIFIED"):
        runtime._build_runtime()


def test_ipc_mode_requires_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(secret_store, "assert_production_backend", lambda: None)
    monkeypatch.setenv("TABVIS_CREDENTIAL_BROKER_MODE", "ipc")
    monkeypatch.delenv("TABVIS_CREDENTIAL_BROKER_ENDPOINT", raising=False)
    with pytest.raises(runtime.ManagedAuthenticationConfigurationError, match="requires"):
        runtime._build_runtime()


def test_cli_composition_injects_runtime_and_trusted_ids(monkeypatch) -> None:
    class FakeRuntime:
        healthy = True

    class Store:
        @staticmethod
        def get_state():
            return {}

        @staticmethod
        def set_state(_update):
            return None

    fake = FakeRuntime()
    runtime.set_managed_authentication_runtime(fake)
    monkeypatch.setenv("TABVIS_AUTHENTICATION_ENABLED", "1")
    try:
        context = _build_tool_use_context(
            tools=[],
            model="test-model",
            mcp_clients=[],
            mcp_resources={},
            agent_definitions={},
            commands=[],
            store=Store(),
            agent_id="agent-1",
            principal_id="principal-1",
            session_id="session-1",
            run_id="run-1",
        )
    finally:
        runtime.set_managed_authentication_runtime(None)

    assert context.authentication_service is fake
    assert context.agent_id == "agent-1"
    assert context.principal_id == "principal-1"
    assert context.run_id == "run-1"
    assert context.browser_session_id == "session-1"
