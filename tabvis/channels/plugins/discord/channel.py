"""DiscordChannel — a Discord channel plugin on the client-loop transport (design §4.2, §4.8).

Discord's inbound is the **Gateway websocket** (``MESSAGE_CREATE`` dispatches); outbound is ordinary
REST (:class:`DiscordClient`). So this is a :class:`ClientLoopChannel`: :meth:`start` runs the gateway
read loop and funnels each message into the inbound pipeline; :meth:`deliver` posts a reply to the
channel the run's conversation is bound to.

The event parse + dispatch (:meth:`_to_inbound`) is unit-tested through an injected source of
``MESSAGE_CREATE`` payloads. The live gateway connection needs a websocket client, which is not a base
dependency — it is imported lazily and raises a clear ``uv sync --extra discord`` hint if missing, so
tests and the rest of the framework never require it.
"""

from __future__ import annotations

import asyncio
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
from tabvis.channels.plugins.discord.client import DiscordClient, DiscordConfig

PLUGIN_ID = "discord"

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# Intents: GUILD_MESSAGES (1<<9) | DIRECT_MESSAGES (1<<12) | MESSAGE_CONTENT (1<<15). MESSAGE_CONTENT
# is privileged and must be enabled in the app's developer portal, or `content` arrives empty.
_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)


class DiscordChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: DiscordConfig,
        *,
        client: DiscordClient | None = None,
        source: AsyncIterable[dict] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else DiscordClient(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DiscordChannel":
        return cls(DiscordConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    async def _run_loop(self) -> None:
        if self._source is not None:  # test / alternative-transport path — MESSAGE_CREATE payloads
            async for event in self._source:
                await self._handle(event)
            return
        await self._gateway_loop()

    async def _handle(self, message_create: dict) -> None:
        message = self._to_inbound(message_create)
        if message is not None:
            await self._submit(self._account_id, message)

    def _to_inbound(self, data: dict) -> InboundMessage | None:
        if not isinstance(data, dict):
            return None
        author = data.get("author") or {}
        author_id = author.get("id")
        if author_id and str(author_id) == str(self._config.bot_user_id):
            return None  # our own message
        if author.get("bot") and not self._config.allow_bots:
            return None  # ignore other bots (avoid bot-to-bot loops) unless explicitly opted in
        content = data.get("content")
        if not content:
            return None  # empty content usually means the MESSAGE_CONTENT intent isn't enabled
        channel_id = data.get("channel_id")
        if not channel_id:
            return None
        return InboundMessage(
            external_event_id=str(data.get("id") or ""),
            external_conversation_id=str(channel_id),  # a channel id addresses both guild channels and DMs
            external_account_ref=self._account_id,
            text=str(content),
            external_user_id=str(author_id) if author_id else None,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        channel_id = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not channel_id:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no channel id for conversation")
        try:
            message_id = await self._client.send_text(channel_id, outbound.text)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    # --- live gateway (optional dependency) ----------------------------------------------------

    async def _gateway_loop(self) -> None:
        """Connect to the Discord Gateway, IDENTIFY, heartbeat, and dispatch MESSAGE_CREATE events.

        The websocket client is an optional extra; without it this raises a clear install hint rather
        than importing at module load (so the plugin stays importable and testable everywhere).
        """
        try:
            import json

            import websockets
        except ImportError as exc:  # optional extra
            raise RuntimeError(
                "The Discord live gateway needs a websocket client. Install it with "
                "`uv sync --extra discord`."
            ) from exc

        async with websockets.connect(_GATEWAY_URL, max_size=None) as socket:
            hello = json.loads(await socket.recv())
            interval = (hello.get("d") or {}).get("heartbeat_interval", 41250) / 1000.0
            last_seq: int | None = None

            async def _heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await socket.send(json.dumps({"op": 1, "d": last_seq}))

            heartbeat = asyncio.ensure_future(_heartbeat())
            try:
                await socket.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": self._config.bot_token,
                        "intents": _INTENTS,
                        "properties": {"os": "linux", "browser": "tabvis", "device": "tabvis"},
                    },
                }))
                async for raw in socket:
                    frame = json.loads(raw)
                    if frame.get("s") is not None:
                        last_seq = frame["s"]
                    if frame.get("op") == 1:  # server-requested heartbeat
                        await socket.send(json.dumps({"op": 1, "d": last_seq}))
                    elif frame.get("op") == 0 and frame.get("t") == "MESSAGE_CREATE":
                        await self._handle(frame.get("d") or {})
            finally:
                heartbeat.cancel()
