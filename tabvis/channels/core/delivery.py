"""Outbound delivery (design §4.5, §4.4).

Projects a gateway outbound message onto a channel, with two contract guarantees:

* **Idempotency** — a ``delivery_id`` is delivered at most once; a repeat returns the stored receipt
  (design §4.5). This is what makes a retried outbound safe.
* **Capability degradation** — a non-final (streaming) chunk to a channel that lacks
  ``stream.incremental`` is skipped; such a channel receives only the final message (design §4.4).

Every attempt records a receipt and emits ``channel.delivery.succeeded`` / ``.failed`` (design §14.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from tabvis.channels.core.contract import (
    CAP_STREAM_INCREMENTAL,
    ChannelPlugin,
    DeliveryReceipt,
    OutboundMessage,
)
from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.events import AGGREGATE_CHANNEL, EventScope, EventType
from tabvis.utils.debug import log_for_debugging


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeliveryService:
    def __init__(self, events: EventStore | None = None) -> None:
        self._events = events or get_event_store()

    async def deliver(self, plugin: ChannelPlugin, outbound: OutboundMessage) -> DeliveryReceipt:
        from tabvis.gateway.store import db

        prior = db.get_delivery(outbound.delivery_id)
        if prior is not None:
            return DeliveryReceipt(
                delivery_id=outbound.delivery_id,
                status="duplicate",
                external_message_id=(prior.get("data", {}) or {}).get("external_message_id")
                if isinstance(prior.get("data"), dict) else prior.get("external_message_id"),
            )

        caps = plugin.manifest.capabilities
        if not outbound.final and CAP_STREAM_INCREMENTAL not in caps:
            # The channel can't stream — drop the partial; only the final message is delivered (§4.4).
            receipt = DeliveryReceipt(outbound.delivery_id, status="skipped", detail="no stream.incremental")
            self._record(outbound, receipt)
            return receipt

        try:
            receipt = await plugin.deliver(outbound)
        except Exception as e:  # noqa: BLE001 - a channel failure is reported, not raised to the caller
            log_for_debugging(f"[CHANNEL] delivery {outbound.delivery_id} failed: {e}")
            receipt = DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(e))

        self._record(outbound, receipt)
        return receipt

    def _record(self, outbound: OutboundMessage, receipt: DeliveryReceipt) -> None:
        from tabvis.gateway.store import db

        db.insert_delivery(
            {
                "delivery_id": receipt.delivery_id,
                "channel_account_id": outbound.channel_account_id,
                "run_id": outbound.run_id,
                "status": receipt.status,
                "created_at": _utc_now(),
                "external_message_id": receipt.external_message_id,
                "detail": receipt.detail,
            }
        )
        event_type = (
            EventType.CHANNEL_DELIVERY_SUCCEEDED if receipt.status in ("succeeded", "skipped", "duplicate")
            else EventType.CHANNEL_DELIVERY_FAILED
        )
        self._events.append(
            AGGREGATE_CHANNEL, outbound.channel_account_id or outbound.conversation_id, event_type,
            scope=EventScope(conversation_id=outbound.conversation_id, run_id=outbound.run_id),
            data={"delivery_id": receipt.delivery_id, "status": receipt.status},
        )
