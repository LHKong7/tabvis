"""TelegramChannel — a Telegram IM channel plugin on the client-loop transport (design §4.2, §4.8).

Telegram has no webhook-by-default and no push socket: the bot **long-polls** ``getUpdates`` and sends
with ``sendMessage`` (both plain HTTPS). So this is a :class:`ClientLoopChannel` — :meth:`start` runs
the poll loop as a background task that funnels each update into the inbound pipeline, and
:meth:`deliver` sends replies addressed to the chat the run's conversation is bound to.

The read source is injectable (``source=`` an async iterable of raw Update dicts) so the loop is
unit-testable without a live bot; the default builds the real ``getUpdates`` loop.

Wiring sketch::

    tg = TelegramChannel.from_env()
    gateway.register_plugin(tg)
    gateway.register_account(ChannelAccount(channel_account_id="ca_telegram", plugin_id="telegram"))
    await gateway.start_plugin("telegram")   # begins long-polling
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterable, Mapping

from tabvis.channels.core.contract import (
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    ChannelManifest,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
)
from tabvis.channels.plugins._platform.loop import ClientLoopChannel
from tabvis.channels.plugins.telegram.client import TelegramClient, TelegramConfig
from tabvis.utils.debug import log_for_debugging

PLUGIN_ID = "telegram"

_MAX_POLL_BACKOFF = 30.0  # cap the reconnect backoff after repeated getUpdates failures


class TelegramChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,
    )

    def __init__(
        self,
        config: TelegramConfig,
        *,
        client: TelegramClient | None = None,
        source: AsyncIterable[dict] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else TelegramClient(config)
        self._source = source
        self._account_id = config.channel_account_id or f"ca_{PLUGIN_ID}"
        self._bot_id: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TelegramChannel":
        return cls(TelegramConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()
        await self._client.aclose()

    # --- read loop ------------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        if self._source is not None:  # test / alternative-transport path
            async for update in self._source:
                await self._handle(update)
            return
        # Live long-poll: learn our own id (to drop echoed messages), then poll from the ack cursor.
        try:
            await self._ensure_bot_id()
        except Exception as exc:  # noqa: BLE001 - a getMe blip must not stop us from ever polling
            log_for_debugging(f"[telegram] getMe failed at startup: {exc}")
        offset: int | None = None
        backoff = 1.0
        while True:
            try:
                updates = await self._client.get_updates(offset=offset, timeout=self._config.poll_timeout)
                backoff = 1.0  # a good poll resets the backoff
            except asyncio.CancelledError:
                raise  # cooperative shutdown — never swallow the cancel
            except Exception as exc:  # noqa: BLE001 - a transient poll error must not kill the loop
                log_for_debugging(f"[telegram] getUpdates failed, retrying in {backoff:.0f}s: {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_POLL_BACKOFF)
                continue
            for update in updates:
                update_id = update.get("update_id")
                if update_id is not None:
                    offset = max(offset or 0, int(update_id) + 1)  # advancing the offset acks the update
                await self._handle(update)

    async def _ensure_bot_id(self) -> None:
        if self._bot_id is None:
            self._bot_id = (await self._client.get_me()).get("id")

    async def _handle(self, update: dict) -> None:
        message = self._to_inbound(update)
        if message is not None:
            await self._submit(self._account_id, message)

    def _to_inbound(self, update: dict) -> InboundMessage | None:
        # message / edited_message / channel_post are all message-bearing; others (callbacks) are skipped.
        message = update.get("message") or update.get("edited_message") or update.get("channel_post")
        if not isinstance(message, dict):
            return None
        text = message.get("text") or message.get("caption")
        if not text:
            return None
        sender = message.get("from") or {}
        if self._bot_id is not None and sender.get("id") == self._bot_id:
            return None  # groups echo the bot's own outbound back through getUpdates — never re-ingest it
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        user_id = sender.get("id")
        if user_id is None:  # channel posts have no `from`; attribute to the broadcasting chat
            user_id = (message.get("sender_chat") or {}).get("id")
        # Enforce the optional allowlist: when set, only messages from listed user ids are accepted.
        if self._config.allowed_users and (user_id is None or str(user_id) not in self._config.allowed_users):
            return None
        return InboundMessage(
            # update_id is the true global dedupe key (message_id is only per-chat unique).
            external_event_id=str(update.get("update_id") or message.get("message_id")),
            external_conversation_id=str(chat_id),
            external_account_ref=self._account_id,
            text=str(text),
            external_user_id=str(user_id) if user_id is not None else None,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        chat_id: Any = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not chat_id:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no chat id for conversation")
        try:
            message_id = await self._client.send_message(chat_id, outbound.text)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)
