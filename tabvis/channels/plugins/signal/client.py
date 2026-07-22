"""Signal transport via the ``signal-cli`` JSON-RPC daemon (line-delimited JSON over a TCP socket).

Signal has no official bot HTTP API; the standard programmatic path is ``signal-cli`` running as a
daemon (``signal-cli -a +<number> daemon --tcp <host>:<port>``), which speaks line-delimited JSON-RPC:
incoming messages arrive as ``receive`` notifications, and ``send`` is a JSON-RPC request written to
the same socket. So this is a stdlib-``asyncio`` socket connection — no third-party library — much like
the IRC channel, but the frames are JSON-RPC objects rather than IRC lines.

Requires a running, registered ``signal-cli`` daemon (an external prerequisite the operator sets up).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

from tabvis.channels.plugins._platform.config import env_required, env_str


@dataclass
class SignalConfig:
    account: str            # this bot's own Signal number (+E.164), used to drop self/sync messages
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 7583    # signal-cli daemon --tcp port
    channel_account_id: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SignalConfig":
        return cls(
            account=env_required("TABVIS_SIGNAL_ACCOUNT", env=env),
            rpc_host=env_str("TABVIS_SIGNAL_RPC_HOST", "127.0.0.1", env=env),
            rpc_port=int(env_str("TABVIS_SIGNAL_RPC_PORT", "7583", env=env) or 7583),
            channel_account_id=env_str("TABVIS_SIGNAL_CHANNEL_ACCOUNT_ID", env=env),
        )


class SignalConnection:
    """The live signal-cli JSON-RPC socket: connect, read notifications, write requests."""

    def __init__(self, config: SignalConfig) -> None:
        self._config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._id = 0

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._config.rpc_host, self._config.rpc_port), timeout=30
        )

    async def read_message(self) -> dict | None:
        if self._reader is None:
            return None
        try:
            raw = await self._reader.readuntil(b"\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            return None
        try:
            message = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return {}
        return message if isinstance(message, dict) else {}

    async def send(self, method: str, params: dict[str, Any]) -> None:
        if self._writer is None:
            raise RuntimeError("signal-cli connection not open")
        self._id += 1
        line = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": f"tabvis-{self._id}"})
        self._writer.write((line + "\n").encode("utf-8"))
        await self._writer.drain()

    async def aclose(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._reader = None
