"""WeComChannel — a 企业微信 / WeCom self-built-app callback channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for WeCom's callback
mode: WeCom POSTs an AES-encrypted XML envelope to the app's callback URL; this plugin verifies the
SHA1 ``msg_signature``, decrypts the envelope, normalizes the message, and sends replies out-of-band
through the WeCom ``message/send`` API. WeCom's verification is its own scheme — a
``sorted(token,timestamp,nonce,encrypt)`` SHA1 signature carried in the *query string*, an
AES-256-CBC ``WXBizMsgCrypt`` body, and a GET ``echostr`` URL-verification handshake — rather than the
framework's plain HMAC, so this plugin declares ``signed_webhooks=False`` and does that verification
itself in :meth:`handle_webhook` before handing a clean :class:`RawInbound` to the gateway.

Two WeCom-specific facts shape this plugin:

* **Everything on the wire is XML; JSON is only the outbound API.** The callback body is encrypted
  XML, and the signature covers the base64 ciphertext string, not the parsed XML.
* **It is DM-only.** A callback message is always user↔app, so there is no group id — the external
  conversation is a synthesized ``"{corp_id}:{user_id}"`` (``ToUserName:FromUserName``), and outbound
  splits it back to the WeCom userid for ``touser``.

Wiring sketch (a transport / HTTP route drives it)::

    wecom = WeComChannel.from_env()
    gateway.register_plugin(wecom)
    gateway.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
    await gateway.start_plugin("wecom")

    # GET (URL verification) and POST (messages) both carry msg_signature/timestamp/nonce in the query:
    result = wecom.handle_webhook(request.query, raw_body)
    if result.challenge is not None:      # GET echostr handshake -> return the DECRYPTED plaintext
        return text_plain(result.challenge, status=200)
    if result.rejected:                   # bad signature / decrypt / body
        return status(403)
    await gateway.receive_webhook("ca_wecom", result.raw)   # dedupe -> bind -> event -> Run
    return text_plain("success", status=200)  # ACK fast; the real reply is pushed via message/send
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
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
from tabvis.channels.plugins.wecom.client import WeComClient, WeComConfig
from tabvis.channels.plugins.wecom.crypto import decrypt_message, verify_signature

PLUGIN_ID = "wecom"

# WeCom callback media is never inline (delivered via MediaId out-of-band), so 64KB is ample; the cap
# bounds the pre-auth XML parse against an oversized-body DoS (the transport should answer 413).
_MAX_BODY = 65536

# Lifecycle events carry no user text and must be dropped rather than turned into a Run.
_IGNORED_EVENTS = {"enter_agent", "subscribe"}


@dataclass
class WeComWebhookResult:
    """What decoding a raw WeCom HTTP callback tells the transport to do next."""

    challenge: str | None = None   # GET url-verification: return this DECRYPTED plaintext as text/plain
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 403; nothing was ingested
    reason: str | None = None


class WeComChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # WeCom's SHA1 scheme is custom; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: WeComConfig, *, client: WeComClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else WeComClient(config)
        self._services: ChannelServices | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WeComChannel":
        return cls(WeComConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, query: Mapping[str, str], raw_body: bytes) -> WeComWebhookResult:
        """Verify + decrypt a raw WeCom callback and say what the transport should do.

        WeCom carries ``msg_signature``/``timestamp``/``nonce`` (and, on GET, ``echostr``) in the
        query string — not headers — so ``query`` is the request's query-param mapping. Any failure
        returns ``rejected=True`` and nothing is ingested.
        """
        q = {k.lower(): v for k, v in (query or {}).items()}
        msg_signature = q.get("msg_signature", "")
        timestamp = q.get("timestamp", "")
        nonce = q.get("nonce", "")

        # GET URL-verification: echostr is itself an encrypted blob — verify, decrypt, echo plaintext.
        echostr = q.get("echostr")
        if echostr is not None:
            if not verify_signature(self._config.token, timestamp, nonce, echostr, msg_signature):
                return WeComWebhookResult(rejected=True, reason="url-verify signature mismatch")
            try:
                plain = decrypt_message(self._config.encoding_aes_key, self._config.corp_id, echostr)
            except Exception as exc:  # noqa: BLE001
                return WeComWebhookResult(rejected=True, reason=f"url-verify decrypt failed: {exc}")
            return WeComWebhookResult(challenge=plain)

        # POST message: the ciphertext lives in <Encrypt> inside the XML body.
        if not raw_body:
            return WeComWebhookResult(rejected=True, reason="empty body")
        if len(raw_body) > _MAX_BODY:
            return WeComWebhookResult(rejected=True, reason="body too large")
        outer = _parse_xml(raw_body)
        if outer is None:
            return WeComWebhookResult(rejected=True, reason="invalid XML body")
        encrypt = outer.get("Encrypt")
        if not encrypt:
            return WeComWebhookResult(rejected=True, reason="missing Encrypt element")

        # The signature covers the base64 ciphertext string, not the parsed XML.
        if not verify_signature(self._config.token, timestamp, nonce, encrypt, msg_signature):
            return WeComWebhookResult(rejected=True, reason="signature mismatch")
        try:
            inner_xml = decrypt_message(self._config.encoding_aes_key, self._config.corp_id, encrypt)
        except Exception as exc:  # noqa: BLE001
            return WeComWebhookResult(rejected=True, reason=f"decrypt failed: {exc}")
        fields = _parse_xml(inner_xml)
        if fields is None:
            return WeComWebhookResult(rejected=True, reason="invalid decrypted XML")

        from_user = str(fields.get("FromUserName") or "")
        # ToUserName is the corp/receive_id; fall back to our configured corp id when absent.
        corp_id = str(fields.get("ToUserName") or self._config.corp_id)
        create_time = str(fields.get("CreateTime") or "")
        # Dedup key: MsgId (WeCom retries on ACK timeout), falling back to sender:create_time.
        msg_id = str(fields.get("MsgId") or "") or f"{from_user}:{create_time}"
        raw = RawInbound(
            external_event_id=msg_id,
            external_conversation_id=f"{corp_id}:{from_user}",  # synthesized 1:1 chat id
            external_account_ref=corp_id,
            payload=fields,
        )
        return WeComWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        msg_type = str(payload.get("MsgType") or "").lower()
        if msg_type not in {"text", "event"}:
            return []  # only text + event are handled; images/voice/etc. arrive out-of-band via MediaId
        if msg_type == "event" and str(payload.get("Event") or "").lower() in _IGNORED_EVENTS:
            return []  # enter_agent / subscribe — lifecycle noise, not a user message

        text = str(payload.get("Content") or "").strip()
        if not text and msg_type == "event":
            text = "/start"  # a bare event (e.g. a menu click) carries no Content — treat as a start
        if not text:
            return []

        from_user = str(payload.get("FromUserName") or "")
        return [
            InboundMessage(
                external_event_id=inbound.external_event_id,
                external_conversation_id=inbound.external_conversation_id,
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=from_user or None,
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        chat_id = self._resolve_chat_id(outbound)
        if not chat_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external chat id for conversation"
            )
        # The bound external id is the synthesized "{corp_id}:{user_id}"; touser is the userid part.
        user_id = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
        try:
            message_id = await self._client.send_text(user_id, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_chat_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the external id is the binding's chat id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- XML parsing -------------------------------------------------------------------------------


def _parse_xml(data: str | bytes) -> dict[str, str] | None:
    """Flatten WeCom's flat ``<xml><Tag>value</Tag>...</xml>`` into a ``{tag: text}`` dict.

    The callback body is parsed *pre-auth* (the ``<Encrypt>`` element must be extracted before the
    signature can be checked). Python's stdlib ``ElementTree`` does not resolve external entities and
    rejects undefined internal ones, so billion-laughs/XXE are not expanded; combined with the
    :data:`_MAX_BODY` cap upstream, that bounds an untrusted-XML parse without a third-party parser.
    """
    try:
        root = ET.fromstring(data)
    except Exception:  # noqa: BLE001 - malformed / hostile XML is a rejection, not a crash
        return None
    return {child.tag: (child.text or "") for child in root}
