"""MatrixChannel — a Matrix IM channel plugin on the client-loop transport (design §4.2, §4.8).

Matrix delivers with an HTTP long-poll ``/sync`` loop (no webhook, no socket), so this is a
:class:`ClientLoopChannel`: :meth:`start` runs the sync loop as a background task and funnels each
``m.room.message`` into the inbound pipeline; :meth:`deliver` PUTs a reply into the room the run's
conversation is bound to. The read source is injectable (an async iterable of timeline events with a
``room_id`` set) so the loop is unit-testable without a homeserver.

Note: the room id is **not** in the event body in a raw ``/sync`` response — it is the key under
``rooms.join`` — so the live loop injects it onto each event before handing it to :meth:`_to_inbound`.
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
from tabvis.channels.plugins.matrix.client import MatrixClient, MatrixConfig

PLUGIN_ID = "matrix"
_STARTUP_GRACE_MS = 5000


class MatrixChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: MatrixConfig,
        *,
        client: MatrixClient | None = None,
        source: AsyncIterable[dict] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else MatrixClient(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"
        self._user_id = config.user_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MatrixChannel":
        return cls(MatrixConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    # --- read loop ------------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        if self._source is not None:  # test / alternative-transport path — events carry room_id already
            async for event in self._source:
                await self._handle(event)
            return
        if not self._user_id:  # resolve our MXID so we can drop our own echoed messages (fail-safe)
            self._user_id = await self._client.whoami()
        since: str | None = None
        first = True
        startup_ms = time.time() * 1000
        while True:
            response = await self._client.sync(since=since, timeout_ms=self._config.sync_timeout_ms)
            joined = ((response.get("rooms") or {}).get("join") or {})
            for room_id, room in joined.items():
                for event in ((room.get("timeline") or {}).get("events") or []):
                    if first:
                        # The initial /sync replays room history; drop anything older than startup.
                        ts = event.get("origin_server_ts") or 0
                        if ts and ts < startup_ms - _STARTUP_GRACE_MS:
                            continue
                    await self._handle({**event, "room_id": room_id})
            since = response.get("next_batch") or since
            first = False

    async def _handle(self, event: dict) -> None:
        message = self._to_inbound(event)
        if message is not None:
            await self._submit(self._account_id, message)

    def _to_inbound(self, event: dict) -> InboundMessage | None:
        if event.get("type") != "m.room.message":
            return None
        content = event.get("content") or {}
        if content.get("msgtype") != "m.text":  # skip m.notice (bot-loop guard), media, etc.
            return None
        if (content.get("m.relates_to") or {}).get("rel_type") == "m.replace":
            return None  # an edit, not a new message
        body = content.get("body")
        if not body:
            return None
        sender = event.get("sender") or ""
        # Drop our own messages; if our MXID is somehow unknown, drop everything (echo-loop fail-safe).
        if not self._user_id or sender.strip().lower() == self._user_id.strip().lower():
            return None
        room_id = event.get("room_id")
        if not room_id:
            return None
        return InboundMessage(
            external_event_id=str(event.get("event_id") or ""),
            external_conversation_id=str(room_id),
            external_account_ref=self._account_id,
            text=str(body),
            external_user_id=sender,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        room_id = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not room_id:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no room id for conversation")
        try:
            # Use the delivery_id as the Matrix txn id → a retried delivery is idempotent server-side too.
            event_id = await self._client.send_text(room_id, outbound.text, txn_id=outbound.delivery_id)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=event_id)
