"""Orchestrator → Broker narrow client (design §14, §5.1, §5.2).

The Agent tool never touches this — the Orchestrator does. The client's job is twofold:

1. **enrich**: turn the Agent's bare :class:`AgentAuthenticationRequest` (profile id only) into an
   internal :class:`AuthenticationRequest` by adding trusted context (task / user / session / agent)
   from the run context. The Agent can never supply these fields (§5.1, §5.2);
2. **transport**: ship the request to the Broker and return the redacted result.

Two transports share one interface: :class:`InProcessBrokerClient` (L0, direct call) and
:class:`SocketBrokerClient` (L1/L2, Unix socket). Neither ever returns a secret, capability or
exception text to the caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import (
    AgentAuthenticationRequest,
    AuthenticationRequest,
    AuthenticationResult,
)


def enrich_request(
    agent_request: AgentAuthenticationRequest,
    *,
    request_id: str,
    browser_session_id: str,
    task_id: str,
    user_id: str,
    agent_id: str,
) -> AuthenticationRequest:
    """Build the internal request from the Agent request + trusted run context (§5.2).

    The trusted fields come from the Orchestrator's run context, NEVER from the Agent — that is what
    stops an Agent forging another task's / user's / session's authentication.
    """
    return AuthenticationRequest(
        request_id=request_id,
        browser_session_id=browser_session_id,
        credential_profile_id=agent_request.credential_profile_id,
        task_id=task_id,
        user_id=user_id,
        agent_id=agent_id,
        requested_at=datetime.now(timezone.utc),
    )


class BrokerClient(Protocol):
    async def authenticate(self, request: AuthenticationRequest) -> AuthenticationResult: ...


class InProcessBrokerClient:
    """L0 transport: call an in-process Broker directly (no isolation; §2.3 L0)."""

    def __init__(self, broker: "object") -> None:
        self._broker = broker

    async def authenticate(self, request: AuthenticationRequest) -> AuthenticationResult:
        return await self._broker.authenticate(request)


class SocketBrokerClient:
    """L1/L2 transport: send the request to the Broker over a Unix domain socket."""

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path

    async def authenticate(self, request: AuthenticationRequest) -> AuthenticationResult:
        import asyncio

        from tabvis.credential_broker.protocol import decode, encode, read_frame, write_frame

        try:
            reader, writer = await asyncio.open_unix_connection(self._socket_path)
        except (OSError, ConnectionError):
            return AuthenticationResult(
                success=False, error_code=AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE.value
            )
        try:
            await write_frame(writer, encode(request.model_dump(mode="json")))
            payload = await read_frame(reader)
            return AuthenticationResult.model_validate(decode(payload))
        except Exception:  # noqa: BLE001 - any transport failure is a redacted internal error
            return AuthenticationResult(
                success=False, error_code=AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
