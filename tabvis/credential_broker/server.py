"""Credential Broker IPC server (design §6.2, §2.3 L1/L2).

Runs the :class:`~tabvis.credential_broker.broker.CredentialBroker` behind a Unix domain socket so the
Broker can live in a separate process from the Agent runtime. Hardening (design §6.2):

* the socket file is created with ``0600`` permissions (owner-only);
* every accepted connection's **peer credentials** (``SO_PEERCRED``) are checked against an allowed uid
  set — a connection from another OS user is refused before a single byte is read;
* the request is validated against the fixed :class:`AuthenticationRequest` schema (extra fields
  rejected), and the response is the redacted :class:`AuthenticationResult`.

This is the L1 boundary (separate process, same OS user by default). L2 additionally runs the Broker
under a distinct OS identity / sandbox — a deployment concern layered on top of this server.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct

from tabvis.authentication.models import AuthenticationRequest, AuthenticationResult
from tabvis.credential_broker.broker import CredentialBroker
from tabvis.credential_broker.protocol import decode, encode, read_frame, write_frame
from tabvis.utils.debug import log_for_debugging


def _peer_uid(sock: socket.socket) -> int | None:
    """Return the connecting peer's uid via SO_PEERCRED, or None if unavailable (non-Linux)."""
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except (OSError, AttributeError):
        return None


class BrokerServer:
    def __init__(
        self,
        broker: CredentialBroker,
        *,
        socket_path: str,
        allowed_uids: set[int] | None = None,
    ) -> None:
        self._broker = broker
        self._socket_path = socket_path
        # Default: only the Broker's own OS user may connect (design §6.2 Peer Credential).
        self._allowed_uids = allowed_uids if allowed_uids is not None else {os.getuid()}
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if os.path.exists(self._socket_path):
            os.unlink(self._socket_path)
        self._server = await asyncio.start_unix_server(self._handle, path=self._socket_path)
        os.chmod(self._socket_path, 0o600)  # owner-only socket file

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if os.path.exists(self._socket_path):
            try:
                os.unlink(self._socket_path)
            except OSError:
                pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            uid = _peer_uid(sock) if sock is not None else None
            if uid is not None and uid not in self._allowed_uids:
                log_for_debugging(f"[BROKER] refused connection from uid={uid}")
                return  # drop without responding
            payload = await read_frame(reader)
            result = await self._dispatch(payload)
            await write_frame(writer, encode(result.model_dump()))
        except (asyncio.IncompleteReadError, ValueError, ConnectionError):
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _dispatch(self, payload: bytes) -> AuthenticationResult:
        try:
            request = AuthenticationRequest.model_validate(decode(payload))
        except Exception:  # noqa: BLE001 - a malformed / extra-field request is a hard reject
            from tabvis.authentication.errors import AuthErrorCode

            return AuthenticationResult(
                success=False, error_code=AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR.value
            )
        return await self._broker.authenticate(request)
