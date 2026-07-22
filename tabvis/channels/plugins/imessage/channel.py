"""IMessageChannel — an iMessage channel plugin on the client-loop transport (design §4.2, §4.8).

Apple iMessage has no pure-Python or webhook path: the live link is Photon Spectrum's gRPC stream,
reached through a local Node **sidecar** the operator runs. From the plugin's side the transport is
loopback HTTP (a long-lived ``GET /inbound`` NDJSON stream + ``POST /send``), so this is a
:class:`ClientLoopChannel`: :meth:`start` consumes the inbound stream and funnels each text message
into the inbound pipeline; :meth:`deliver` posts a reply addressed to the space the run's conversation
is bound to. The read source is injectable so parsing/dispatch are testable without a running sidecar.

Requires the macOS Photon sidecar running and reachable (``TABVIS_IMESSAGE_SIDECAR_URL`` /
``TABVIS_IMESSAGE_SIDECAR_TOKEN``); the sidecar owns the Photon credentials and the gRPC stream.
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
from tabvis.channels.plugins.imessage.client import IMessageConfig, IMessageSidecarClient

PLUGIN_ID = "imessage"


class IMessageChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: IMessageConfig,
        *,
        client: IMessageSidecarClient | None = None,
        source: AsyncIterable[dict] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else IMessageSidecarClient(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "IMessageChannel":
        return cls(IMessageConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    async def _run_loop(self) -> None:
        # The client's stream_inbound() already reconnects with backoff; the injected source is finite.
        stream = self._source if self._source is not None else self._client.stream_inbound()
        async for event in stream:
            await self._handle(event)

    async def _handle(self, event: dict) -> None:
        message = self._to_inbound(event)
        if message is not None:
            await self._submit(self._account_id, message)

    def _to_inbound(self, event: dict) -> InboundMessage | None:
        if not isinstance(event, dict):
            return None
        # The sidecar forwards only inbound; if a direction slips through, drop our own outbound echo.
        direction = event.get("direction")
        if direction is not None and direction != "inbound":
            return None
        content = event.get("content") or {}
        if content.get("type") != "text":
            return None  # attachments/reactions/group-bundles are out of scope for a plain-text bot
        text = content.get("text")
        if not text:
            return None
        space = event.get("space") or {}
        space_id = space.get("id")
        if not space_id:
            return None
        sender = event.get("sender") or {}
        user = sender.get("id") or space.get("phone") or space_id
        message_id = event.get("messageId") or f"imessage-{time.time_ns()}"
        return InboundMessage(
            external_event_id=str(message_id),
            external_conversation_id=str(space_id),
            external_account_ref=self._account_id,
            text=str(text),
            external_user_id=str(user) if user else None,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        space_id = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not space_id:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no space id for conversation")
        try:
            message_id = await self._client.send_text(space_id, outbound.text, fmt=self._config.send_format)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)
