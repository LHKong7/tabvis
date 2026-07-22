"""FeishuChannel — a Feishu / Lark IM channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for Feishu's
``im.message.receive_v1`` events. Feishu's webhook verification is its own scheme — a
``timestamp+nonce+key`` signature, an optional AES-encrypted body, and a ``url_verification``
challenge — rather than the framework's plain HMAC, so this plugin declares ``signed_webhooks=False``
and does that verification itself in :meth:`handle_webhook` before handing a clean
:class:`RawInbound` to the gateway's inbound pipeline. Outbound text is sent through the Feishu
messages API, addressed to the chat the run's conversation is bound to.

Wiring sketch (a transport / HTTP route drives it)::

    feishu = FeishuChannel.from_env()
    gateway.register_plugin(feishu)
    gateway.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
    await gateway.start_plugin("feishu")

    # in the POST handler for the Feishu event-subscription URL:
    result = feishu.handle_webhook(request.headers, raw_body)
    if result.challenge is not None:      # url_verification handshake
        return json({"challenge": result.challenge})
    if result.rejected:                   # bad signature / token / body
        return status(401)
    await gateway.receive_webhook("ca_feishu", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)
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
from tabvis.channels.plugins.feishu.client import FeishuClient, FeishuConfig
from tabvis.channels.plugins.feishu.crypto import decrypt_envelope, verify_signature

PLUGIN_ID = "feishu"
EVENT_MESSAGE_RECEIVED = "im.message.receive_v1"


@dataclass
class FeishuWebhookResult:
    """What decoding a raw Feishu HTTP webhook tells the transport to do next."""

    challenge: str | None = None   # echo ``{"challenge": ...}`` with HTTP 200
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class FeishuChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # Feishu's scheme is custom; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: FeishuConfig, *, client: FeishuClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else FeishuClient(config)
        self._services: ChannelServices | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeishuChannel":
        return cls(FeishuConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> FeishuWebhookResult:
        """Verify + decrypt a raw Feishu webhook and say what the transport should do.

        Order mirrors Feishu's own contract: parse → decrypt (if encrypted) → verification token →
        ``url_verification`` challenge → signature. Any failure returns ``rejected=True`` and nothing
        is ingested.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return FeishuWebhookResult(rejected=True, reason="invalid JSON body")

        # Encrypted mode: decrypt the envelope into the real event payload.
        if isinstance(payload, dict) and payload.get("encrypt"):
            try:
                payload = json.loads(decrypt_envelope(self._config.encrypt_key, payload["encrypt"]))
            except Exception as exc:  # noqa: BLE001
                return FeishuWebhookResult(rejected=True, reason=f"decrypt failed: {exc}")

        if not isinstance(payload, dict):
            return FeishuWebhookResult(rejected=True, reason="unexpected payload shape")

        # Verification token — header.token for v2 events, top-level token for url_verification.
        if self._config.verification_token:
            token = str((payload.get("header") or {}).get("token") or payload.get("token") or "")
            if not constant_time_eq(token, self._config.verification_token):
                return FeishuWebhookResult(rejected=True, reason="verification token mismatch")

        # URL verification handshake — echo the challenge (after the token check above).
        if payload.get("type") == "url_verification":
            return FeishuWebhookResult(challenge=str(payload.get("challenge", "")))

        # Signature — only enforced when an Encrypt Key is configured.
        if self._config.encrypt_key:
            ok = verify_signature(
                self._config.encrypt_key,
                lower.get("x-lark-request-timestamp", ""),
                lower.get("x-lark-request-nonce", ""),
                raw_body,
                lower.get("x-lark-signature"),
            )
            if not ok:
                return FeishuWebhookResult(rejected=True, reason="signature mismatch")

        header = payload.get("header") or {}
        message = (payload.get("event") or {}).get("message") or {}
        raw = RawInbound(
            external_event_id=str(header.get("event_id") or ""),
            external_conversation_id=str(message.get("chat_id") or ""),
            external_account_ref=str(header.get("app_id") or self._config.app_id),
            payload=payload,
        )
        return FeishuWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        header = payload.get("header") or {}
        if (header.get("event_type") or payload.get("type")) != EVENT_MESSAGE_RECEIVED:
            return []  # url_verification and other event types produce no inbound message
        event = payload.get("event") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") in {"bot", "app"}:
            return []  # never react to our own or another bot's messages
        message = event.get("message") or {}
        text = _extract_text(message)
        if not text:
            return []
        sender_id = sender.get("sender_id") or {}
        external_user_id = (
            sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
        )
        return [
            InboundMessage(
                external_event_id=str(header.get("event_id") or inbound.external_event_id),
                external_conversation_id=str(message.get("chat_id") or inbound.external_conversation_id),
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=external_user_id,
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        chat_id = self._resolve_chat_id(outbound)
        if not chat_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external chat id for conversation"
            )
        try:
            message_id = await self._client.send_text(chat_id, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_chat_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the Feishu chat_id is the binding's external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- inbound text extraction -------------------------------------------------------------------


def _load_content(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _extract_text(message: dict) -> str:
    msg_type = message.get("message_type") or ""
    content = _load_content(message.get("content"))
    if msg_type == "text":
        return _clean_mentions(str(content.get("text", "")), message.get("mentions"))
    if msg_type == "post":
        return _extract_post_text(content)
    if msg_type in {"image", "media", "file", "audio"}:
        name = content.get("file_name") or ""
        return f"[{msg_type}{': ' + name if name else ''}]"
    # Unknown type — best effort, else a typed placeholder so the message isn't silently dropped.
    return _clean_mentions(str(content.get("text", "")), message.get("mentions")) or f"[{msg_type or 'message'}]"


def _clean_mentions(text: str, mentions: Any) -> str:
    """Replace Feishu's ``@_user_N`` / ``@_all`` placeholders with readable names."""
    if not text:
        return ""
    result = text.replace("@_all", "@all")
    for mention in mentions or []:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        name = mention.get("name") or ""
        if key:
            result = result.replace(key, f"@{name}" if name else "")
    return result.strip()


def _extract_post_text(content: dict) -> str:
    """Flatten a Feishu rich-text ``post`` payload to plain text (title + text/at nodes)."""
    node = content
    if isinstance(node.get("post"), dict):
        node = node["post"]
    if "content" not in node:  # unwrap a locale layer (zh_cn / en_us / …)
        for value in node.values():
            if isinstance(value, dict) and "content" in value:
                node = value
                break
    parts: list[str] = []
    title = node.get("title")
    if title:
        parts.append(str(title))
    rows = node.get("content")
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, list):
                continue
            segment: list[str] = []
            for element in row:
                if not isinstance(element, dict):
                    continue
                tag = element.get("tag")
                if tag in {"text", "a", "md"}:
                    segment.append(str(element.get("text", "")))
                elif tag == "at":
                    segment.append("@" + str(element.get("user_name") or element.get("user_id") or "all"))
            if segment:
                parts.append("".join(segment))
    return "\n".join(part for part in parts if part).strip()
