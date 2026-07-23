"""SimpleX Chat config + send client.

SimpleX is deliberately unlike the REST platforms in this package. There is no HTTP API, no OAuth,
no bearer token: a single **persistent local WebSocket** to a ``simplex-chat`` daemon is *both* the
inbound event stream and the outbound command channel. Reachability to that socket is the only
"credential" — the daemon runs locally and trusts whoever can reach it. So this client does not
subclass :class:`RestChannelClient` (there is nothing to authenticate and no host to talk to over
httpx); it just mints command frames and writes them to a WebSocket.

Outbound sends are **fire-and-forget**: the daemon does not reliably return a correlated reply for a
chat command, so :meth:`SimpleXClient.send_command` treats a successful socket write as success and
returns the frame's ``corrId`` as the message reference. Each command carries a ``corrId`` prefixed
:data:`_CORR_PREFIX` so the read loop can recognize (and drop) the echo of our own sends.

The ``websockets`` PyPI package is unavoidable for the live socket, but httpx has no WebSocket client
and we may not add a dependency, so it is imported lazily via :func:`require_websockets`: the plugin
stays importable (and unit-testable through an injected ``sender``) without it, and only the live
connection raises a clear install hint.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping

from tabvis.channels.plugins._platform.config import env_bool, env_str

# Every command we send is tagged with this corrId prefix so the read loop can drop the daemon's
# echo of our own outbound messages (belt to the chatDir directSnd/groupSnd suspenders).
_CORR_PREFIX = "tabvis-"

# SimpleX imposes no hard message-length limit; this is a soft guard kept for documentation parity
# with the reference adapter. Sends are not truncated.
MAX_MESSAGE_LENGTH = 8000

# The daemon's default local WebSocket endpoint. Not a secret — it is where the socket lives.
_DEFAULT_WS_URL = "ws://127.0.0.1:5225"

# An async callable that writes one already-encoded JSON frame to the transport. Injected in tests.
Sender = Callable[[str], Awaitable[None]]


def _split_csv(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def require_websockets():
    """Import ``websockets`` lazily, raising a clear install hint when it is absent.

    httpx (our only guaranteed async HTTP dep) has no WebSocket client, and SimpleX's live socket
    needs one. Rather than adding a dependency we defer the import to the live path so the plugin is
    discoverable — and fully unit-testable via an injected source/sender — without the package.
    """
    try:
        import websockets  # noqa: PLC0415 - deferred so the plugin imports without the optional dep
    except ImportError as exc:  # pragma: no cover - exercised only without the optional extra
        raise RuntimeError(
            "the SimpleX live connection needs the `websockets` package (httpx has no WebSocket "
            "client); install it with `uv sync --extra simplex`"
        ) from exc
    return websockets


@dataclass
class SimpleXConfig:
    """One configured SimpleX daemon connection. All values come from ``TABVIS_SIMPLEX_*``."""

    ws_url: str = _DEFAULT_WS_URL          # local daemon WebSocket; reachability *is* the auth
    channel_account_id: str = "ca_simplex" # the single account this loop serves (see channel._run_loop)
    allowed_users: frozenset[str] = field(default_factory=frozenset)  # contactIds / display names
    allow_all_users: bool = False          # dev switch: skip the DM allowlist entirely
    group_allowed: frozenset[str] = field(default_factory=frozenset)  # groupIds, or "*" for any

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SimpleXConfig":
        return cls(
            ws_url=env_str("TABVIS_SIMPLEX_WS_URL", _DEFAULT_WS_URL, env=env).rstrip("/"),
            channel_account_id=env_str("TABVIS_SIMPLEX_CHANNEL_ACCOUNT_ID", "ca_simplex", env=env),
            allowed_users=_split_csv(env_str("TABVIS_SIMPLEX_ALLOWED_USERS", env=env)),
            allow_all_users=env_bool("TABVIS_SIMPLEX_ALLOW_ALL_USERS", False, env=env),
            group_allowed=_split_csv(env_str("TABVIS_SIMPLEX_GROUP_ALLOWED", env=env)),
        )


class SimpleXClient:
    """Sends chat commands to the SimpleX daemon over its WebSocket (fire-and-forget).

    The command *string* (``@id text`` for a DM, ``/_send #id json [...]`` for a group) is built by
    the channel — addressing differs by chat type — and this client only wraps it in the ``corrId``
    envelope and writes it. In tests a ``sender`` is injected to capture frames; the default opens an
    ephemeral connection per send (mirroring the reference's out-of-process ``_standalone_send``),
    which keeps :meth:`deliver` independent of the read loop's long-lived socket.
    """

    def __init__(self, config: SimpleXConfig, *, sender: Sender | None = None) -> None:
        self._config = config
        self._sender = sender
        self._counter = 0

    def _next_corr_id(self) -> str:
        self._counter += 1
        return f"{_CORR_PREFIX}{self._counter}-{int(time.time() * 1000)}"

    async def send_command(self, cmd: str) -> str:
        """Encode ``cmd`` into a ``{"corrId","cmd"}`` frame, write it, and return the corrId.

        A successful write is treated as success: the daemon returns no reliable reply for a chat
        command, so there is nothing to await (see module docstring).
        """
        corr_id = self._next_corr_id()
        await self._send(json.dumps({"corrId": corr_id, "cmd": cmd}))
        return corr_id

    async def _send(self, frame: str) -> None:
        if self._sender is not None:
            await self._sender(frame)
            return
        # Live path: an ephemeral connection per send. The daemon needs a beat to process the command
        # before the socket closes, hence the short sleep the reference adapter also uses.
        websockets = require_websockets()
        async with websockets.connect(self._config.ws_url, open_timeout=10, close_timeout=5) as ws:
            await ws.send(frame)
            await asyncio.sleep(0.5)

    async def aclose(self) -> None:
        return None
