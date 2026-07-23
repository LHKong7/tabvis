"""WhatsAppChannel — a WhatsApp Cloud (Meta Graph) channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for WhatsApp Cloud's
``messages`` webhook. Meta's webhook verification is its own scheme — a ``hub.*`` GET subscription
handshake and an ``X-Hub-Signature-256`` HMAC over the raw POST body — rather than the framework's
plain HMAC, so this plugin declares ``signed_webhooks=False`` and does that verification itself in
:meth:`handle_webhook` before handing a clean :class:`RawInbound` to the gateway's inbound pipeline.
Outbound text is sent through the Graph messages API, addressed to the wa_id the run's conversation is
bound to (a WhatsApp DM has no separate chat id — the conversation *is* the sender's wa_id).

Wiring sketch (a transport / HTTP route drives it)::

    wa = WhatsAppChannel.from_env()
    gateway.register_plugin(wa)
    gateway.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
    await gateway.start_plugin("whatsapp")

    # GET handler for the Meta subscription handshake:
    result = wa.handle_webhook(request.headers, b"", params=request.query)
    if result.challenge is not None:      # echo it verbatim as text/plain, HTTP 200
        return text(result.challenge)
    if result.rejected:                   # bad mode/token → 400/403 (or 503 if unconfigured)
        return status(403)

    # POST handler for the message webhook:
    result = wa.handle_webhook(request.headers, raw_body)
    if result.rejected:                   # bad/missing signature → 401 (or 503 if unconfigured)
        return status(401)
    await gateway.receive_webhook("ca_wa", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)                    # ALWAYS 200 on a valid signed request, else Meta retries
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
from tabvis.channels.plugins._platform.webhook import constant_time_eq
from tabvis.channels.plugins.whatsapp.client import WhatsAppClient, WhatsAppConfig
from tabvis.channels.plugins.whatsapp.crypto import verify_signature

PLUGIN_ID = "whatsapp"
OBJECT_TYPE = "whatsapp_business_account"
MESSAGES_FIELD = "messages"
SIGNATURE_HEADER = "x-hub-signature-256"
# Meta caps a webhook body at 3 MB; a larger POST is refused before we even hash it.
WEBHOOK_MAX_BODY_BYTES = 3 * 1024 * 1024


@dataclass
class WhatsAppWebhookResult:
    """What decoding a raw WhatsApp HTTP webhook tells the transport to do next."""

    challenge: str | None = None   # echo verbatim as text/plain with HTTP 200 (GET handshake)
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 400/401/403/503; nothing was ingested
    reason: str | None = None


class WhatsAppChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # WhatsApp's scheme is custom; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: WhatsAppConfig, *, client: WhatsAppClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else WhatsAppClient(config)
        self._services: ChannelServices | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WhatsAppChannel":
        return cls(WhatsAppConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(
        self,
        headers: Mapping[str, str],
        raw_body: bytes,
        params: Mapping[str, str] | None = None,
    ) -> WhatsAppWebhookResult:
        """Verify a raw WhatsApp webhook and say what the transport should do.

        Meta uses two routes on one URL: a **GET** subscription handshake (the ``hub.*`` query params)
        and the **POST** message webhook (a signed JSON body). We dispatch on the query params — a
        ``hub.mode``/``hub.challenge`` present means the handshake — and otherwise treat ``raw_body`` as
        the signed event. Any failure returns ``rejected=True`` and nothing is ingested.
        """
        params = params or {}
        if params.get("hub.mode") is not None or "hub.challenge" in params:
            return self._handle_verify(params)
        return self._handle_event(headers, raw_body or b"")

    def _handle_verify(self, params: Mapping[str, str]) -> WhatsAppWebhookResult:
        """GET subscription handshake — echo ``hub.challenge`` only after a constant-time token match."""
        if not self._config.verify_token:
            return WhatsAppWebhookResult(rejected=True, reason="verify_token not configured")  # 503
        if params.get("hub.mode") != "subscribe":
            return WhatsAppWebhookResult(rejected=True, reason="bad mode")  # 400
        provided = str(params.get("hub.verify_token") or "")
        if not constant_time_eq(provided, self._config.verify_token):
            return WhatsAppWebhookResult(rejected=True, reason="verify_token mismatch")  # 403
        challenge = params.get("hub.challenge")
        if not challenge:
            return WhatsAppWebhookResult(rejected=True, reason="missing challenge")  # 400
        return WhatsAppWebhookResult(challenge=str(challenge))

    def _handle_event(self, headers: Mapping[str, str], raw_body: bytes) -> WhatsAppWebhookResult:
        """POST message webhook — verify HMAC over the *raw* bytes, then parse, in that order."""
        if not self._config.app_secret:
            return WhatsAppWebhookResult(rejected=True, reason="app_secret not configured")  # 503
        if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
            return WhatsAppWebhookResult(rejected=True, reason="body too large")  # 413

        lower = {k.lower(): v for k, v in headers.items()}
        # Verify BEFORE parsing: the MAC covers the exact bytes Meta sent, so re-serialized JSON — even
        # if identical in meaning — would hash differently and fail. Hash the untouched body.
        if not verify_signature(self._config.app_secret, raw_body, lower.get(SIGNATURE_HEADER)):
            return WhatsAppWebhookResult(rejected=True, reason="signature mismatch")  # 401

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return WhatsAppWebhookResult(rejected=True, reason="invalid JSON body")  # 400
        if not isinstance(payload, dict) or payload.get("object") != OBJECT_TYPE:
            return WhatsAppWebhookResult(rejected=True, reason="unexpected payload shape")  # 400

        # The RawInbound event id / conversation id are cosmetic here — dedupe happens per-message on
        # the wamid in normalize — but we surface the first message so the envelope is meaningful.
        first = _first_message(payload)
        raw = RawInbound(
            external_event_id=str(first.get("id") or ""),
            external_conversation_id=str(first.get("from") or ""),
            external_account_ref=str(_phone_number_id(payload) or self._config.phone_number_id),
            payload=payload,
        )
        return WhatsAppWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        if payload.get("object") != OBJECT_TYPE:
            return []  # not a WhatsApp business event — nothing to ingest
        messages: list[InboundMessage] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                # Only "messages" changes carry inbound user turns; delivery "statuses" and template
                # updates ride other fields and produce no message (they are logged, never dispatched).
                if not isinstance(change, dict) or change.get("field") != MESSAGES_FIELD:
                    continue
                value = change.get("value") or {}
                own_number = str((value.get("metadata") or {}).get("display_phone_number") or "")
                for message in value.get("messages") or []:
                    normalized = self._normalize_one(inbound, message, own_number)
                    if normalized is not None:
                        messages.append(normalized)
        return messages

    def _normalize_one(
        self, inbound: RawInbound, message: Any, own_number: str
    ) -> InboundMessage | None:
        if not isinstance(message, dict):
            return None
        # Groups aren't implemented on Cloud; a group-shaped payload carries a `chat` object — drop it.
        if message.get("chat"):
            return None
        sender = str(message.get("from") or "")
        # Never react to our own number (a defensive self-echo guard) or to Stories/Channels broadcasts.
        if not sender or (own_number and sender == own_number) or _is_broadcast_chat(sender):
            return None
        text = _extract_text(message)
        if not text:
            return None
        wamid = str(message.get("id") or "")
        return InboundMessage(
            external_event_id=wamid or inbound.external_event_id,  # wamid is the dedupe key (Meta retries 7d)
            external_conversation_id=sender,  # a DM has no chat entity — chat_id == the sender wa_id
            external_account_ref=inbound.external_account_ref,
            text=text,
            external_user_id=sender,
        )

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        recipient = self._resolve_recipient(outbound)
        if not recipient:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external recipient for conversation"
            )
        try:
            message_id = await self._client.send_text(recipient, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_recipient(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the WhatsApp recipient wa_id is the binding's
        # external id (which, for a DM, is the sender's wa_id we recorded on the inbound message).
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- payload walking + inbound text extraction -------------------------------------------------


def _first_message(payload: dict) -> dict:
    """The first inbound message anywhere in the batch (used only for the RawInbound envelope)."""
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != MESSAGES_FIELD:
                continue
            for message in (change.get("value") or {}).get("messages") or []:
                if isinstance(message, dict):
                    return message
    return {}


def _phone_number_id(payload: dict) -> str:
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            metadata = (change.get("value") or {}).get("metadata") or {}
            if metadata.get("phone_number_id"):
                return str(metadata["phone_number_id"])
    return ""


def _is_broadcast_chat(wa_id: str) -> bool:
    """Drop WhatsApp pseudo-chats (Status/Stories, Channels) that are not real conversations."""
    return wa_id == "status@broadcast" or wa_id.endswith("@broadcast") or wa_id.endswith("@newsletter")


def _extract_text(message: dict) -> str:
    msg_type = message.get("type") or ""
    if msg_type == "text":
        return str((message.get("text") or {}).get("body", "")).strip()
    if msg_type == "interactive":
        return _extract_interactive(message.get("interactive") or {})
    if msg_type == "button":
        return str((message.get("button") or {}).get("text", "")).strip()
    if msg_type in {"image", "video", "audio", "voice", "document", "sticker"}:
        section = message.get(msg_type) or {}
        label = str(section.get("caption") or section.get("filename") or "")
        return f"[{msg_type}{': ' + label if label else ''}]"
    # Unknown type — a typed placeholder so the message isn't silently dropped.
    return f"[{msg_type or 'message'}]"


def _extract_interactive(interactive: dict) -> str:
    """Flatten an interactive reply (button_reply / list_reply) to the chosen option's title."""
    kind = interactive.get("type")
    node = interactive.get(kind) if kind else None
    if isinstance(node, dict):
        return str(node.get("title") or node.get("id") or "").strip()
    return ""
