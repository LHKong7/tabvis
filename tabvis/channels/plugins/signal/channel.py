"""SignalChannel — a Signal channel plugin on the client-loop transport (design §4.2, §4.8).

Reads ``receive`` notifications from the ``signal-cli`` JSON-RPC daemon and funnels each text message
into the inbound pipeline; :meth:`deliver` writes a ``send`` request back on the same socket. A
:class:`ClientLoopChannel` with an injectable source (async iterable of JSON-RPC notification dicts)
so the parse + dispatch are unit-testable without a running daemon.

Signal has no per-message ack we wait on here, so :meth:`deliver` is fire-and-forget (like IRC):
success means the request was written without error.
"""

from __future__ import annotations

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
from tabvis.channels.plugins.signal.client import SignalConfig, SignalConnection

PLUGIN_ID = "signal"


class SignalChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: SignalConfig,
        *,
        client: SignalConnection | None = None,
        source: AsyncIterable[dict] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else SignalConnection(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SignalChannel":
        return cls(SignalConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    async def _run_loop(self) -> None:
        if self._source is not None:  # test / alternative-transport path — JSON-RPC notification dicts
            async for message in self._source:
                await self._process(message)
            return
        await self._client.connect()
        while True:
            message = await self._client.read_message()
            if message is None:
                break  # socket closed
            await self._process(message)

    async def _process(self, message: dict) -> None:
        # signal-cli delivers incoming messages as JSON-RPC "receive" notifications.
        if message.get("method") != "receive":
            return
        inbound = self._to_inbound(message.get("params") or {})
        if inbound is not None:
            await self._submit(self._account_id, inbound)

    def _to_inbound(self, params: dict) -> InboundMessage | None:
        envelope = params.get("envelope") or {}
        data_message = envelope.get("dataMessage") or {}
        text = data_message.get("message")
        if not text:
            return None  # receipts, typing, sync, reactions carry no dataMessage text
        source = envelope.get("sourceNumber") or envelope.get("source") or ""
        if source == self._config.account:
            return None  # our own number (a sync of what we sent) — never re-ingest
        group_id = (data_message.get("groupInfo") or {}).get("groupId")
        conversation = group_id or source
        if not conversation:
            return None
        timestamp = envelope.get("timestamp")
        return InboundMessage(
            external_event_id=f"{timestamp}:{source}",  # Signal has no message id; timestamp+sender is unique
            external_conversation_id=str(conversation),
            external_account_ref=self._account_id,
            text=str(text),
            external_user_id=source or None,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        conversation = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not conversation:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no recipient for conversation")
        # A +E.164 target is a 1:1 recipient; anything else is a group id.
        if str(conversation).startswith("+"):
            params = {"recipient": [conversation], "message": outbound.text}
        else:
            params = {"groupId": conversation, "message": outbound.text}
        try:
            await self._client.send("send", params)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=str(outbound.delivery_id))
