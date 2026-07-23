"""IrcChannel — an IRC channel plugin on the client-loop transport (design §4.2, §4.8).

IRC is a persistent CRLF TCP socket, so this is a :class:`ClientLoopChannel`: :meth:`start` opens the
connection and reads lines forever, answering ``PING`` keepalives and funneling addressed ``PRIVMSG``
lines into the inbound pipeline; :meth:`deliver` writes ``PRIVMSG`` back on the same socket. The line
source is injectable (an async iterable of raw IRC lines) so parsing + dispatch are unit-testable
without a server; the default opens the real socket.

Channel addressing mirrors IRC etiquette: a message in a ``#channel`` is only handled if it is
addressed to the bot (``nick:`` / ``nick,`` / ``nick ``), and that prefix is stripped; DMs are always
handled.
"""

from __future__ import annotations

import time
from typing import AsyncIterable, Mapping

from tabvis.channels.core.contract import (
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    ChannelManifest,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
)
from tabvis.channels.plugins._platform.loop import ClientLoopChannel
from tabvis.channels.plugins.irc.client import (
    IrcConfig,
    IrcConnection,
    extract_nick,
    is_channel_target,
    parse_irc_message,
    strip_control,
)

PLUGIN_ID = "irc"


class IrcChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: IrcConfig,
        *,
        client: IrcConnection | None = None,
        source: AsyncIterable[str] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else IrcConnection(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"
        self._nick = config.nickname

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "IrcChannel":
        return cls(IrcConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    # --- read loop ------------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        if self._source is not None:  # test / alternative-transport path
            async for line in self._source:
                await self._process_line(line)
            return
        await self._client.connect()
        self._nick = self._client.current_nick or self._nick
        while True:
            line = await self._client.read_line()
            if line is None:
                break  # socket closed — the base marks the channel degraded
            await self._process_line(line)

    async def _process_line(self, raw: str) -> None:
        message = parse_irc_message(raw)
        command = message["command"].upper()
        if command == "PING":  # mandatory keepalive, or the server drops us
            await self._client.send_line(f"PONG :{message['params'][0] if message['params'] else ''}")
            return
        if command == "NICK" and extract_nick(message["prefix"]).lower() == self._nick.lower():
            if message["params"]:
                self._nick = message["params"][0]
            return
        if command == "PRIVMSG":
            inbound = self._to_inbound(raw)
            if inbound is not None:
                await self._submit(self._account_id, inbound)

    def _to_inbound(self, raw: str) -> InboundMessage | None:
        message = parse_irc_message(raw)
        if message["command"].upper() != "PRIVMSG" or len(message["params"]) < 2:
            return None
        sender = extract_nick(message["prefix"])
        if sender.lower() == self._nick.lower():
            return None  # our own message echoed on some networks
        target, text = message["params"][0], message["params"][1]

        # CTCP: render /me actions, drop other CTCP.
        if text.startswith("\x01"):
            if text.startswith("\x01ACTION ") and text.endswith("\x01"):
                text = f"* {sender} {text[8:-1]}"
            else:
                return None

        if is_channel_target(target):
            conversation = target
            addressed = self._strip_address(text)
            if addressed is None:
                return None  # an unaddressed channel message — not for us
            text = addressed
        else:
            conversation = sender  # a DM: reply to the sender's nick, not the bot's own nick target

        if not text:
            return None
        return InboundMessage(
            # IRC has no native message id; synthesize a monotonic one (the framework dedupes on it).
            external_event_id=f"irc-{time.time_ns()}",
            external_conversation_id=conversation,
            external_account_ref=self._account_id,
            text=text,
            external_user_id=sender,
        )

    def _strip_address(self, text: str) -> str | None:
        """In a channel, require ``nick:``/``nick,``/``nick `` addressing; return the text with it removed."""
        low = text.lower()
        for sep in (f"{self._nick.lower()}:", f"{self._nick.lower()},", f"{self._nick.lower()} "):
            if low.startswith(sep):
                return text[len(sep):].strip()
        return None

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        conversation = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not conversation:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no target for conversation")
        try:
            for line in _split_lines(strip_control(outbound.text), self._config.max_line_chars):
                await self._client.send_line(f"PRIVMSG {conversation} :{line}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        # IRC gives no ack for PRIVMSG — success is "the writes did not raise".
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=str(outbound.delivery_id))


def _split_lines(text: str, limit: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        while len(paragraph) > limit:
            lines.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        lines.append(paragraph)
    return [line for line in lines if line] or [""]
