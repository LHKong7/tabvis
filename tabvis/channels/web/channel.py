"""WebChannel — the console/HTTP channel (design §4.8).

Implemented first to prove the contract (design §4.8): it wraps today's local console behavior as a
first-class channel. It has no webhook (messages arrive already-authenticated over the local HTTP
surface), streams incrementally (the SSE event feed), and delivers by leaning on that same feed, so
``deliver`` is a no-op receipt — the Web console reads Run events directly rather than being pushed to.
"""

from __future__ import annotations

from tabvis.channels.core.contract import (
    CAP_STREAM_INCREMENTAL,
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    ChannelHealth,
    ChannelManifest,
    ChannelServices,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
    RawInbound,
)

PLUGIN_ID = "web"


class WebChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="1.0.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND, CAP_STREAM_INCREMENTAL}),
        signed_webhooks=False,
    )

    def __init__(self) -> None:
        self._services: ChannelServices | None = None

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        text = str(inbound.payload.get("text", ""))
        return [
            InboundMessage(
                external_event_id=inbound.external_event_id,
                external_conversation_id=inbound.external_conversation_id,
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=inbound.payload.get("user"),
            )
        ]

    async def submit_console_message(self, channel_account_id: str, external_conversation_id: str, text: str):
        """Console entry point: submit a typed message through the same inbound pipeline as any channel."""
        if self._services is None:
            raise RuntimeError("WebChannel not started")
        message = InboundMessage(
            external_event_id=f"web:{external_conversation_id}:{abs(hash(text)) & 0xFFFFFFFF:08x}",
            external_conversation_id=external_conversation_id,
            external_account_ref=channel_account_id,
            text=text,
        )
        return await self._services.submit_inbound(channel_account_id, message)

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        # The Web console consumes Run events over SSE, so there is nothing to push here.
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", detail="delivered via SSE feed")

    async def acknowledge(self, external_event_id: str) -> None:
        return None
