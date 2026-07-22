"""LineChannel — a LINE Messaging API channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for LINE webhook events.
LINE's verification is its own scheme — ``base64(HMAC_SHA256(channel_secret, raw_body))`` against the
``X-Line-Signature`` header, over the *exact raw bytes* — rather than the framework's plain-HMAC-hex
gate, so this plugin declares ``signed_webhooks=False`` and verifies itself in :meth:`handle_webhook`
before handing a clean :class:`RawInbound` to the gateway's inbound pipeline. There is no
``url_verification`` challenge and no encrypted envelope; the console's "Verify" button just posts an
empty ``{"events": []}`` body with a valid signature, which flows through as a signed no-op.

Outbound is LINE's two-tier send: an inbound event yields a single-use, ~60s reply token stashed
per-chat; :meth:`deliver` spends it on the *free* reply endpoint when fresh and falls back to the
*metered* push endpoint otherwise (or when the token is already spent).

Wiring sketch (a transport / HTTP route drives it)::

    line = LineChannel.from_env()
    gateway.register_plugin(line)
    gateway.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
    await gateway.start_plugin("line")

    # in the POST handler for the LINE webhook URL:
    result = line.handle_webhook(request.headers, raw_body)
    if result.rejected:                   # bad X-Line-Signature / malformed body
        return status(401)
    await gateway.receive_webhook("ca_line", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)   # always "ok" on a verified body, even the empty Verify ping
"""

from __future__ import annotations

import json
import time
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
from tabvis.channels.plugins.line.client import LineClient, LineConfig
from tabvis.channels.plugins.line.crypto import verify_line_signature

PLUGIN_ID = "line"
EVENT_MESSAGE = "message"
# LINE's reply token is single-use with a ~60s TTL; cap our stash a touch below that (adapter parity).
_REPLY_TOKEN_TTL_SECONDS = 50.0
_MEDIA_TYPES = frozenset({"image", "audio", "video", "file", "sticker", "location"})


@dataclass
class LineWebhookResult:
    """What decoding a raw LINE HTTP webhook tells the transport to do next.

    ``challenge`` is carried for parity with the other webhook plugins but is always ``None`` here —
    LINE has no ``url_verification`` handshake; its "Verify" ping is a signed empty-events body that
    normalizes to nothing and returns 200.
    """

    challenge: str | None = None   # unused: LINE has no challenge handshake
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class LineChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # LINE's scheme is base64-HMAC; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: LineConfig, *, client: LineClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else LineClient(config)
        self._services: ChannelServices | None = None
        self._bot_user_id = config.bot_user_id
        # Per-chat single-use reply tokens: chat_id -> (token, monotonic_expiry).
        self._reply_tokens: dict[str, tuple[str, float]] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LineChannel":
        return cls(LineConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services
        # Fetch our own userId once so normalize can drop self-echoes. LINE does not actually echo the
        # bot's own messages, so this is defensive: if /bot/info is unavailable, filtering is skipped.
        if not self._bot_user_id:
            fetch = getattr(self._client, "get_bot_info", None)
            if fetch is not None:
                try:
                    self._bot_user_id = await fetch() or ""
                except Exception:  # noqa: BLE001
                    self._bot_user_id = ""

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> LineWebhookResult:
        """Verify a raw LINE webhook and say what the transport should do.

        LINE signs the *raw bytes*, so the signature is checked before (and independently of) parsing;
        a bad signature or malformed body returns ``rejected=True`` and nothing is ingested. A verified
        body — including the empty "Verify" ping — produces a :class:`RawInbound` carrying the whole
        payload for :meth:`normalize` to expand (LINE bundles several events per POST).
        """
        lower = {k.lower(): v for k, v in headers.items()}
        if not verify_line_signature(self._config.channel_secret, raw_body, lower.get("x-line-signature")):
            return LineWebhookResult(rejected=True, reason="signature mismatch")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return LineWebhookResult(rejected=True, reason="invalid JSON body")
        if not isinstance(payload, dict):
            return LineWebhookResult(rejected=True, reason="unexpected payload shape")

        events = payload.get("events") or []
        first = events[0] if events and isinstance(events[0], dict) else {}
        chat_id, _ = _resolve_chat(first.get("source") or {})
        raw = RawInbound(
            external_event_id=str(first.get("webhookEventId") or ""),
            external_conversation_id=chat_id,
            external_account_ref=str(payload.get("destination") or self._config.bot_user_id or ""),
            payload=payload,
        )
        return LineWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        events = payload.get("events")
        if not isinstance(events, list):
            return []  # the empty Verify ping (and any non-event body) produces no inbound message
        messages: list[InboundMessage] = []
        for event in events:
            if not isinstance(event, dict) or event.get("type") != EVENT_MESSAGE:
                continue  # only message events become runs; postback/follow/join/… are ignored
            source = event.get("source") or {}
            sender_user_id = str(source.get("userId") or "")
            if self._bot_user_id and sender_user_id == self._bot_user_id:
                continue  # never react to our own echoed messages
            chat_id, _ = _resolve_chat(source)
            reply_token = str(event.get("replyToken") or "")
            if chat_id and reply_token:
                self._stash_reply_token(chat_id, reply_token)  # so deliver() can answer for free
            text = _extract_text(event.get("message") or {})
            if not text:
                continue
            message = event.get("message") or {}
            messages.append(
                InboundMessage(
                    # webhookEventId is THE dedup key (LINE re-delivers at least once); the message id
                    # is a fallback so an id-less event still keys the pipeline.
                    external_event_id=str(event.get("webhookEventId") or message.get("id") or ""),
                    external_conversation_id=chat_id or inbound.external_conversation_id,
                    external_account_ref=inbound.external_account_ref,
                    text=text,
                    external_user_id=sender_user_id or None,
                )
            )
        return messages

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        chat_id = self._resolve_chat_id(outbound)
        if not chat_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external chat id for conversation"
            )
        reply_token = self._consume_reply_token(chat_id)
        try:
            if reply_token:
                try:
                    message_id = await self._client.reply_text(reply_token, outbound.text)
                except Exception:  # noqa: BLE001 - token single-use/expired: fall back to the metered push
                    message_id = await self._client.push_text(chat_id, outbound.text)
            else:
                message_id = await self._client.push_text(chat_id, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    # --- reply-token stash (single-use, TTL-bounded) -------------------------------------------

    def _stash_reply_token(self, chat_id: str, reply_token: str) -> None:
        self._reply_tokens[chat_id] = (reply_token, time.monotonic() + _REPLY_TOKEN_TTL_SECONDS)

    def _consume_reply_token(self, chat_id: str) -> str | None:
        entry = self._reply_tokens.pop(chat_id, None)  # single-use: popped whether or not it's fresh
        if entry is None:
            return None
        token, expiry = entry
        return token if time.monotonic() < expiry else None

    def _resolve_chat_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the LINE chat id is the binding's external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- inbound helpers ---------------------------------------------------------------------------


def _resolve_chat(source: dict) -> tuple[str, str]:
    """Return ``(chat_id, chat_type)`` from a LINE event ``source`` block.

    LINE has three id namespaces — the outbound target is the group/room id for those, else the user id::

        {"type": "user",  "userId":  "U..."}                 -> (U..., "dm")
        {"type": "group", "groupId": "C...", "userId": "U..."} -> (C..., "group")
        {"type": "room",  "roomId":  "R...", "userId": "U..."} -> (R..., "room")
    """
    src_type = source.get("type", "")
    if src_type == "group":
        return str(source.get("groupId") or ""), "group"
    if src_type == "room":
        return str(source.get("roomId") or ""), "room"
    if src_type == "user":
        return str(source.get("userId") or ""), "dm"
    return "", "dm"


def _extract_text(message: dict) -> str:
    """Plain text for a text message; a typed placeholder for media so it isn't silently dropped."""
    msg_type = message.get("type") or ""
    if msg_type == "text":
        return str(message.get("text", "") or "").strip()
    if msg_type in _MEDIA_TYPES:
        # LINE media carries no caption inline (the binary lives behind api-data.line.me).
        return f"[{msg_type}]"
    return ""
