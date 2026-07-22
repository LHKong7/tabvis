"""IRC line protocol + a stdlib-asyncio socket connection.

IRC has no HTTP, no REST, no webhook, no SDK — it is a persistent CRLF-delimited TCP (optionally TLS)
socket. This module holds the line parser (shared, pure, testable) and :class:`IrcConnection`, which
owns the ``asyncio`` streams: registration handshake, ``read_line``/``send_line``, and teardown. The
channel drives it; tests inject a fake with the same ``send_line`` surface and feed raw lines directly.
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_bool, env_required, env_str

_CONTROL = str.maketrans({"\r": " ", "\n": " ", "\x00": ""})


def parse_irc_message(raw: str) -> dict:
    """Parse one IRC line into ``{prefix, command, params}`` (RFC 1459 grammar)."""
    prefix = ""
    rest = raw.strip("\r\n")
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    trailing = None
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    parts = rest.split()
    command = parts[0] if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return {"prefix": prefix, "command": command, "params": params}


def extract_nick(prefix: str) -> str:
    """The nick from a ``nick!user@host`` prefix."""
    return prefix.split("!", 1)[0]


def is_channel_target(target: str) -> bool:
    return bool(target) and target[0] in "#&+!"


def strip_control(text: str) -> str:
    """Neutralize CR/LF/NUL so message content can't inject extra IRC commands."""
    return text.translate(_CONTROL)


@dataclass
class IrcConfig:
    server: str
    channels: tuple[str, ...] = field(default_factory=tuple)
    nickname: str = "tabvis-bot"
    port: int = 6697
    use_tls: bool = True
    server_password: str = ""
    nickserv_password: str = ""
    channel_account_id: str = ""
    max_line_chars: int = 450

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "IrcConfig":
        return cls(
            server=env_required("TABVIS_IRC_SERVER", env=env),
            channels=tuple(c.strip() for c in env_str("TABVIS_IRC_CHANNEL", env=env).split(",") if c.strip()),
            nickname=env_str("TABVIS_IRC_NICKNAME", "tabvis-bot", env=env),
            port=int(env_str("TABVIS_IRC_PORT", "6697", env=env) or 6697),
            use_tls=env_bool("TABVIS_IRC_USE_TLS", True, env=env),
            server_password=env_str("TABVIS_IRC_SERVER_PASSWORD", env=env),
            nickserv_password=env_str("TABVIS_IRC_NICKSERV_PASSWORD", env=env),
            channel_account_id=env_str("TABVIS_IRC_CHANNEL_ACCOUNT_ID", env=env),
        )


class IrcConnection:
    """The live IRC socket: registration handshake, line read/write, teardown."""

    def __init__(self, config: IrcConfig) -> None:
        self._config = config
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.current_nick = config.nickname

    async def send_line(self, line: str) -> None:
        if self._writer is None:
            raise RuntimeError("IRC connection not open")
        self._writer.write((strip_control(line) + "\r\n").encode("utf-8"))
        await self._writer.drain()

    async def read_line(self) -> str | None:
        if self._reader is None:
            return None
        try:
            raw = await self._reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            return None
        return raw.decode("utf-8", errors="replace")

    async def connect(self) -> None:
        context = ssl.create_default_context() if self._config.use_tls else None
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._config.server, self._config.port, ssl=context), timeout=30
        )
        if self._config.server_password:
            await self.send_line(f"PASS {self._config.server_password}")
        await self.send_line(f"NICK {self.current_nick}")
        await self.send_line(f"USER {self.current_nick} 0 * :Tabvis Agent")
        await self._await_registration()
        if self._config.nickserv_password:
            await self.send_line(f"PRIVMSG NickServ :IDENTIFY {self._config.nickserv_password}")
            await asyncio.sleep(2)
        for channel in self._config.channels:
            await self.send_line(f"JOIN {channel}")

    async def _await_registration(self) -> None:
        # Read until RPL_WELCOME (001), answering PING and retrying a taken nick (433) during the wait.
        suffix = 0
        while True:
            line = await self.read_line()
            if line is None:
                raise RuntimeError("IRC connection closed during registration")
            message = parse_irc_message(line)
            command = message["command"].upper()
            if command == "PING":
                await self.send_line(f"PONG :{message['params'][0] if message['params'] else ''}")
            elif command == "001":
                if message["params"]:
                    self.current_nick = message["params"][0]
                return
            elif command == "433":  # nick in use — append underscores/index and retry
                suffix += 1
                self.current_nick = f"{self._config.nickname}_{suffix}" if suffix > 1 else f"{self._config.nickname}_"
                await self.send_line(f"NICK {self.current_nick}")

    async def aclose(self) -> None:
        if self._writer is not None:
            try:
                await self.send_line("QUIT :bye")
            except Exception:  # noqa: BLE001
                pass
            self._writer.close()
            self._writer = None
            self._reader = None
