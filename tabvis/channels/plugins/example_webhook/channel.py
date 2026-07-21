"""ExampleWebhookChannel — a signed-webhook proof channel (design §4.8).

Normalizes a signed JSON webhook body ``{"event_id", "conversation", "user", "text"}`` into an inbound
message, and records outbound deliveries in memory so a test can assert what the gateway pushed. The
signature check itself lives in :class:`ChannelGateway.receive_webhook`; this plugin just declares that
its webhooks are signed.
"""

from __future__ import annotations

from tabvis.channels.core.contract import (
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

PLUGIN_ID = "example_webhook"


class ExampleWebhookChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),  # no stream.incremental
        signed_webhooks=True,
    )

    def __init__(self) -> None:
        self.delivered: list[OutboundMessage] = []
        self.acknowledged: list[str] = []
        self._started = False

    async def start(self, services: ChannelServices) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._started else "stopped")

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        p = inbound.payload
        return [
            InboundMessage(
                external_event_id=str(p.get("event_id", inbound.external_event_id)),
                external_conversation_id=str(p.get("conversation", inbound.external_conversation_id)),
                external_account_ref=inbound.external_account_ref,
                text=str(p.get("text", "")),
                external_user_id=p.get("user"),
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        self.delivered.append(outbound)
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=f"ext_{len(self.delivered)}")

    async def acknowledge(self, external_event_id: str) -> None:
        self.acknowledged.append(external_event_id)
