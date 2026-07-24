"""Broker IPC server + socket client round-trip (design §6.2, §2.3 L1)."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest

from tabvis.authentication.broker_client import SocketBrokerClient, enrich_request
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import AgentAuthenticationRequest, AuthenticationRequest
from tabvis.credential_broker.broker import CredentialBroker, new_request_id
from tabvis.credential_broker.secrets.memory import MemorySecretProvider
from tabvis.credential_broker.server import BrokerServer

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Unix domain sockets / SO_PEERCRED are POSIX-only"
)


def _make_broker(browser_cls, make_profile):
    profile = make_profile()
    provider = MemorySecretProvider({"sec_user": "alice", "sec_pass": "hunter2xyz"})
    browser = browser_cls()

    def lookup(pid, uid):
        return profile if (pid == profile.id and uid == profile.owner_user_id) else None

    return CredentialBroker(
        provider=provider, profile_lookup=lookup, browser_provider=lambda sid: browser
    )


def _request() -> AuthenticationRequest:
    return AuthenticationRequest(
        request_id=new_request_id(),
        browser_session_id="b1",
        credential_profile_id="p1",
        task_id="t1",
        user_id="u1",
        agent_id="a1",
        requested_at=datetime.now(timezone.utc),
    )


def test_socket_round_trip(tmp_path, browser_cls, make_profile) -> None:
    async def scenario() -> None:
        sock_path = str(tmp_path / "broker.sock")
        broker = _make_broker(browser_cls, make_profile)
        server = BrokerServer(broker, socket_path=sock_path)
        await server.start()
        # socket file is owner-only
        assert oct(os.stat(sock_path).st_mode & 0o777) == oct(0o600)
        try:
            client = SocketBrokerClient(sock_path)
            result = await client.authenticate(_request())
            assert result.success
            assert result.authenticated_origin == "https://accounts.example.com"
        finally:
            await server.stop()
        assert not os.path.exists(sock_path)

    asyncio.run(scenario())


def test_extra_fields_rejected_over_ipc(tmp_path, browser_cls, make_profile) -> None:
    async def scenario() -> None:
        from tabvis.credential_broker.protocol import decode, encode, read_frame, write_frame

        sock_path = str(tmp_path / "broker2.sock")
        server = BrokerServer(_make_broker(browser_cls, make_profile), socket_path=sock_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(sock_path)
            # a request with a smuggled secret field must be rejected by the fixed schema
            payload = {
                "request_id": "r1",
                "browser_session_id": "b1",
                "credential_profile_id": "p1",
                "task_id": "t1",
                "user_id": "u1",
                "agent_id": "a1",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "password": "smuggled",
            }
            await write_frame(writer, encode(payload))
            resp = decode(await read_frame(reader))
            writer.close()
            assert resp["success"] is False
            assert resp["error_code"] == AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_enrich_request_adds_trusted_context() -> None:
    agent_req = AgentAuthenticationRequest(credential_profile_id="p1")
    req = enrich_request(
        agent_req,
        request_id="r1",
        browser_session_id="b1",
        task_id="t1",
        user_id="u1",
        agent_id="a1",
    )
    assert req.credential_profile_id == "p1"
    assert req.user_id == "u1" and req.task_id == "t1" and req.browser_session_id == "b1"
