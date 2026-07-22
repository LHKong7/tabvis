"""QQChannel — a QQ official-bot channel plugin (webhook transport, design §4.2, §4.8).

Verifies QQ's Ed25519-signed webhook, answers the op-13 callback validation handshake, and normalizes
group / C2C / guild @-message events. Not derived from Hermes (which has no QQ adapter) — built against
Tencent's official QQ bot v2 webhook + API.

Because a conversation's kind (group vs C2C vs guild channel) determines the send endpoint, the
normalized ``external_conversation_id`` is prefixed (``group:``/``c2c:``/``channel:``). The id of the
triggering message is remembered per conversation so the reply can be sent as a *passive* reply (which
QQ allows freely), since the outbound side only carries the internal conversation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

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
from tabvis.channels.plugins.qq.client import QQClient, QQConfig
from tabvis.channels.plugins.qq.crypto import sign_validation, verify_event

PLUGIN_ID = "qq"
_MESSAGE_EVENTS = frozenset({"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE"})
_OP_VALIDATION = 13


@dataclass
class QQWebhookResult:
    challenge: str | None = None
    raw: RawInbound | None = None
    rejected: bool = False
    reason: str | None = None
    validation: dict | None = None  # the op-13 handshake response body, returned verbatim as JSON


class QQChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # QQ's Ed25519 scheme is custom; verified here, not by the gateway gate
    )

    def __init__(self, config: QQConfig, *, client: QQClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else QQClient(config)
        self._services: ChannelServices | None = None
        self._last_msg_id: dict[str, str] = {}  # conversation -> last inbound message id (passive reply)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "QQChannel":
        return cls(QQConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook ------------------------------------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> QQWebhookResult:
        lower = {k.lower(): v for k, v in headers.items()}
        if not verify_event(
            self._config.secret,
            lower.get("x-signature-timestamp", ""),
            raw_body,
            lower.get("x-signature-ed25519"),
        ):
            return QQWebhookResult(rejected=True, reason="ed25519 signature mismatch")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return QQWebhookResult(rejected=True, reason="invalid JSON body")

        if payload.get("op") == _OP_VALIDATION:  # callback URL validation handshake
            data = payload.get("d") or {}
            signature = sign_validation(self._config.secret, data.get("event_ts", ""), data.get("plain_token", ""))
            return QQWebhookResult(validation={"plain_token": data.get("plain_token", ""), "signature": signature})

        return QQWebhookResult(
            raw=RawInbound(
                external_event_id=str(payload.get("id") or ""),
                external_conversation_id="",  # authoritative id is derived in normalize (kind-prefixed)
                external_account_ref=self._config.app_id,
                payload=payload,
            )
        )

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        event_type = payload.get("t")
        if event_type not in _MESSAGE_EVENTS:
            return []
        data = payload.get("d") or {}
        content = (data.get("content") or "").strip()  # @-messages arrive with a leading space
        if not content:
            return []
        author = data.get("author") or {}
        message_id = data.get("id")

        if event_type == "GROUP_AT_MESSAGE_CREATE":
            conversation = f"group:{data.get('group_openid')}"
            user = author.get("member_openid")
        elif event_type == "C2C_MESSAGE_CREATE":
            conversation = f"c2c:{author.get('user_openid')}"
            user = author.get("user_openid")
        else:  # AT_MESSAGE_CREATE — a guild channel @-message
            conversation = f"channel:{data.get('channel_id')}"
            user = author.get("id")

        if message_id:
            self._last_msg_id[conversation] = message_id  # remember for the passive reply
        return [
            InboundMessage(
                external_event_id=str(payload.get("id") or message_id or ""),
                external_conversation_id=conversation,
                external_account_ref=inbound.external_account_ref,
                text=content,
                external_user_id=str(user) if user else None,
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        conversation = (
            self._services.resolve_external_conversation(outbound.conversation_id)
            if self._services is not None
            else None
        )
        if not conversation:
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail="no target for conversation")
        kind, _, target = str(conversation).partition(":")
        msg_id = self._last_msg_id.get(conversation)  # reply passively to the triggering message
        try:
            if kind == "group":
                external_id = await self._client.send_group(target, outbound.text, msg_id=msg_id)
            elif kind == "c2c":
                external_id = await self._client.send_c2c(target, outbound.text, msg_id=msg_id)
            elif kind == "channel":
                external_id = await self._client.send_channel(target, outbound.text, msg_id=msg_id)
            else:
                return DeliveryReceipt(outbound.delivery_id, status="failed", detail=f"unknown target kind {kind!r}")
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=external_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None
