"""DingTalkChannel — a 钉钉 DingTalk IM channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for DingTalk chatbot
messages. DingTalk's *live* bot uses Stream Mode (an outbound WebSocket the ``dingtalk-stream`` SDK
owns) — unreachable with ``stdlib + httpx`` — so this plugin instead speaks DingTalk's HTTP
outgoing-robot callback: DingTalk POSTs each message to our endpoint with a ``timestamp`` + ``sign``
header pair. That's DingTalk's own scheme (base64 HMAC-SHA256 over ``timestamp+secret``), not the
framework's plain HMAC-over-body, so this plugin declares ``signed_webhooks=False`` and verifies it
itself in :meth:`handle_webhook` before handing a clean :class:`RawInbound` to the gateway. Outbound
text is sent through the DingTalk robot OpenAPI, addressed to the conversation the run is bound to.

Wiring sketch (a transport / HTTP route drives it)::

    dingtalk = DingTalkChannel.from_env()
    gateway.register_plugin(dingtalk)
    gateway.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
    await gateway.start_plugin("dingtalk")

    # in the POST handler for the DingTalk outgoing-robot callback URL:
    result = dingtalk.handle_webhook(request.headers, raw_body)
    if result.rejected:                   # bad signature / stale / bad body
        return status(401)
    await gateway.receive_webhook("ca_dingtalk", result.raw)   # dedupe -> bind -> event -> Run
    return json({"msgtype": "empty"})     # DingTalk expects a 200 (ACK); a real reply goes via deliver()
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

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
from tabvis.channels.plugins.dingtalk.client import DingTalkClient, DingTalkConfig
from tabvis.channels.plugins.dingtalk.crypto import verify_signature

PLUGIN_ID = "dingtalk"


@dataclass
class DingTalkWebhookResult:
    """What decoding a raw DingTalk HTTP webhook tells the transport to do next.

    ``challenge`` is kept for structural parity with the other webhook channels, but DingTalk's
    outgoing-robot callback has **no** ``url_verification`` handshake — it is never populated here.
    """

    challenge: str | None = None   # unused for DingTalk (no challenge handshake); kept for parity
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class DingTalkChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # DingTalk's scheme is custom; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: DingTalkConfig, *, client: DingTalkClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else DingTalkClient(config)
        self._services: ChannelServices | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DingTalkChannel":
        return cls(DingTalkConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> DingTalkWebhookResult:
        """Verify a raw DingTalk outgoing-robot callback and say what the transport should do.

        Order: signature (``timestamp`` + ``sign``) → JSON parse. The signature covers only the
        ``timestamp+secret`` pair (DingTalk's documented scheme — it does not bind the body), so it is
        checked first purely to authenticate the caller and reject replays; the body is trusted only
        once that passes. Any failure returns ``rejected=True`` and nothing is ingested.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        if not verify_signature(self._config.client_secret, lower.get("timestamp", ""), lower.get("sign")):
            return DingTalkWebhookResult(rejected=True, reason="signature mismatch or stale timestamp")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return DingTalkWebhookResult(rejected=True, reason="invalid JSON body")
        if not isinstance(payload, dict):
            return DingTalkWebhookResult(rejected=True, reason="unexpected payload shape")

        # Wire keys are camelCase; ``msgId`` is the dedupe key, ``conversationId`` the reply routing key.
        raw = RawInbound(
            external_event_id=str(payload.get("msgId") or payload.get("messageId") or ""),
            external_conversation_id=str(payload.get("conversationId") or ""),
            external_account_ref=str(payload.get("robotCode") or self._config.client_id),
            payload=payload,
        )
        return DingTalkWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        if not payload.get("msgtype"):
            return []  # not a chatbot message push (e.g. a system/control callback)
        if _is_from_bot(payload):
            return []  # never react to our own bot's messages (self-loop guard)
        text = _extract_text(payload)
        if not text:
            return []
        external_user_id = str(payload.get("senderStaffId") or payload.get("senderId") or "") or None
        return [
            InboundMessage(
                external_event_id=str(
                    payload.get("msgId") or payload.get("messageId") or inbound.external_event_id
                ),
                external_conversation_id=str(
                    payload.get("conversationId") or inbound.external_conversation_id
                ),
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=external_user_id,
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        conversation_id = self._resolve_conversation_id(outbound)
        if not conversation_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external conversation id for conversation"
            )
        try:
            message_key = await self._client.send_text(conversation_id, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_key)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_conversation_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the DingTalk openConversationId is the
        # binding's external id (a group message's conversationId is its openConversationId).
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- inbound text extraction -------------------------------------------------------------------


def _is_from_bot(payload: dict) -> bool:
    """True when a push is the bot's own message.

    Stream Mode never echoes the bot, so the reference adapter relies only on msgId dedup — but the
    facts warn that a mode which *can* echo needs an explicit sender filter. DingTalk stamps the
    receiving robot's own user id as ``chatbotUserId``; a self-authored message carries the same value
    as its sender, so that comparison mirrors Feishu's ``sender_type in {bot, app}`` check.
    """
    bot_id = str(payload.get("chatbotUserId") or "")
    sender = str(payload.get("senderStaffId") or payload.get("senderId") or "")
    return bool(bot_id) and sender == bot_id


def _extract_text(payload: dict) -> str:
    """Pull plain text out of a DingTalk message push.

    The payload here is raw wire JSON (camelCase), so ``text`` is a plain dict and ``text.content`` is
    safe to read directly — no risk of the SDK ``TextContent`` repr leak the reference guards against.
    DingTalk mentions are structural (``isInAtList`` / ``atUsers``), so ``@handles`` are deliberately
    **not** stripped from the text (stripping them would corrupt emails / SSH URLs).
    """
    msgtype = payload.get("msgtype") or ""
    if msgtype == "text":
        return _text_content(payload.get("text"))
    if msgtype in {"richText", "richTextContent"}:
        return _extract_rich_text(payload)
    # Some pushes carry a text block alongside another msgtype — try it, else a typed placeholder so
    # the message isn't silently dropped.
    inline = _text_content(payload.get("text"))
    if inline:
        return inline
    if msgtype in {"picture", "image", "audio", "video", "file"}:
        return f"[{msgtype}]"
    return ""


def _text_content(text: Any) -> str:
    if isinstance(text, dict):
        return str(text.get("content", "")).strip()
    if isinstance(text, str):
        return text.strip()
    return ""


def _extract_rich_text(payload: dict) -> str:
    """Flatten a DingTalk ``richText`` payload to plain text (concatenate its text parts)."""
    content = payload.get("content")
    parts: Any = None
    if isinstance(content, dict):
        parts = content.get("richText") or content.get("rich_text_list")
    if parts is None:
        parts = payload.get("richText") or payload.get("richTextContent")
    segments: list[str] = []
    for part in parts or []:
        if isinstance(part, dict):
            piece = part.get("text") or part.get("content")
            if piece:
                segments.append(str(piece))
    return "".join(segments).strip()
