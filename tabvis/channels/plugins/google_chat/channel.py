"""GoogleChatChannel — a Google Chat channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for Google Chat's
``MESSAGE`` events delivered over an authenticated HTTP callback. Google Chat's webhook verification is
its own scheme — **not** the framework's plain HMAC, and unlike Feishu there is no AES envelope and no
``url_verification`` challenge: Google signs each POST with a Google-issued OIDC ID token (an RS256
JWT) in ``Authorization: Bearer``. The channel therefore declares ``signed_webhooks=False`` and does
that verification itself in :meth:`handle_webhook` — verify the JWT (signature/``iss``/``exp``/``aud``)
and then check the ``email`` claim against the configured Google caller service account — before handing
a clean :class:`RawInbound` to the gateway's inbound pipeline. Note the token authenticates the *caller*
(that Google sent this), not the body bytes (there is no body signature); body integrity rests on TLS,
which is exactly Google Chat's model. Outbound text is created in the space the run's conversation is
bound to, via the Chat REST API.

Wiring sketch (a transport / HTTP route drives it)::

    gchat = GoogleChatChannel.from_env()
    gateway.register_plugin(gchat)
    gateway.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
    await gateway.start_plugin("google_chat")

    # in the POST handler for the Google Chat events callback URL:
    result = gchat.handle_webhook(request.headers, raw_body)
    if result.rejected:                   # bad / missing / unexpected bearer identity
        return status(401)
    await gateway.receive_webhook("ca_gchat", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)                    # non-MESSAGE events normalize to nothing and just 200
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
from tabvis.channels.plugins.google_chat.client import GoogleChatClient, GoogleChatConfig
from tabvis.channels.plugins.google_chat.crypto import (
    GoogleCertsCache,
    KeyResolver,
    verify_google_id_token,
)

PLUGIN_ID = "google_chat"
EVENT_MESSAGE = "MESSAGE"  # the only event type that dispatches to the agent (adapter.py:1300-1302)


@dataclass
class GoogleChatWebhookResult:
    """What decoding a raw Google Chat HTTP webhook tells the transport to do next.

    ``challenge`` exists only for structural parity with the other webhook channels — Google Chat has
    **no** ``url_verification`` handshake, so it is always ``None`` here.
    """

    challenge: str | None = None   # always None for Google Chat (no challenge handshake)
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class GoogleChatChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # Google's OIDC-bearer scheme is custom; verified in handle_webhook
    )

    def __init__(
        self,
        config: GoogleChatConfig,
        *,
        client: GoogleChatClient | None = None,
        resolve_key: KeyResolver | None = None,
    ) -> None:
        self._config = config
        self._client = client if client is not None else GoogleChatClient(config)
        self._services: ChannelServices | None = None
        # The key source for inbound JWT verification. Production fetches Google's JWKS (cached ~300s);
        # tests inject a static resolver. We only own (and must close) the cache when we built it.
        if resolve_key is not None:
            self._resolve_key = resolve_key
            self._certs: GoogleCertsCache | None = None
        else:
            self._certs = GoogleCertsCache()
            self._resolve_key = self._certs.get_key

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GoogleChatChannel":
        return cls(GoogleChatConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        if self._certs is not None:
            self._certs.close()
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> GoogleChatWebhookResult:
        """Verify the Google OIDC bearer and decode the event; say what the transport should do.

        Order mirrors Hermes' ``verify_http_event_request``: the bearer is verified *first* and on its
        own (it is signed over the JWT, not the body), then the body is parsed. Any failure returns
        ``rejected=True`` and nothing is ingested. There is no challenge handshake to answer.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        ok, reason = self._verify_bearer(lower.get("authorization", ""))
        if not ok:
            return GoogleChatWebhookResult(rejected=True, reason=reason)

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return GoogleChatWebhookResult(rejected=True, reason="invalid JSON body")
        if not isinstance(payload, dict):
            return GoogleChatWebhookResult(rejected=True, reason="unexpected payload shape")

        message, space = _extract_message_payload(payload)
        raw = RawInbound(
            # Dedupe key = the message resource name (adapter.py:1934, 1469). Non-MESSAGE events have
            # no message and normalize to nothing, so an empty id here never reaches the dedupe ledger.
            external_event_id=str(message.get("name") or ""),
            external_conversation_id=str(space.get("name") or ""),
            external_account_ref=self._config.service_account_email or PLUGIN_ID,
            payload=payload,
        )
        return GoogleChatWebhookResult(raw=raw)

    def _verify_bearer(self, auth_header: str) -> tuple[bool, str]:
        """Verify the ``Authorization: Bearer <id_token>`` header, mirroring ``verify_http_event_request``."""
        if not self._config.audience or not self._config.caller_service_account_emails:
            return False, "google_chat_http_events_not_configured"
        if not auth_header.startswith("Bearer "):
            return False, "missing_google_bearer"
        token = auth_header[7:].strip()
        if not token:
            return False, "missing_google_bearer"
        try:
            claims = verify_google_id_token(
                token, audience=self._config.audience, resolve_key=self._resolve_key
            )
        except Exception:  # noqa: BLE001 - any verification failure is a rejected request
            return False, "invalid_google_bearer"
        # The ``email`` claim is the Google-side caller SA; a comma-separated allowlist is supported.
        expected = {e.strip().lower() for e in self._config.caller_service_account_emails if e.strip()}
        claim_email = str(claims.get("email") or "").strip().lower()
        if not claim_email or claim_email not in expected:
            return False, "unexpected_google_bearer_identity"
        return True, ""

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        if _event_type(payload) != EVENT_MESSAGE:
            return []  # ADDED_TO_SPACE / CARD_CLICKED / … produce no inbound message
        message, space = _extract_message_payload(payload)
        sender = message.get("sender") or {}
        if str(sender.get("type") or "").upper() == "BOT":
            return []  # never react to our own or another bot's messages (adapter.py:1501-1503)
        text = _extract_text(message)
        if not text:
            return []
        # The sender email is the canonical id (allowlist/session); the users/{id} name is the alt.
        external_user_id = sender.get("email") or sender.get("name")
        return [
            InboundMessage(
                external_event_id=str(message.get("name") or inbound.external_event_id),
                external_conversation_id=str(space.get("name") or inbound.external_conversation_id),
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=external_user_id,
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        space_name = self._resolve_space(outbound)
        if not space_name:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external space for conversation"
            )
        try:
            message_name = await self._client.send_text(space_name, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_name)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_space(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the Chat space (spaces/AAAA) is the binding's
        # external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- inbound event extraction ------------------------------------------------------------------


def _extract_message_payload(envelope: dict) -> tuple[dict, dict]:
    """Return ``(message, space)`` from either accepted envelope shape.

    Format 1 (Workspace add-on) nests them under ``envelope["chat"]["messagePayload"]`` (adapter.py
    :1290-1295); Format 2 (native Chat API) puts them at the top level. The space can also live inside
    the message, so we fall back to that.
    """
    chat = envelope.get("chat")
    if isinstance(chat, dict) and isinstance(chat.get("messagePayload"), dict):
        payload = chat["messagePayload"]
        message = payload.get("message") or {}
        space = payload.get("space") or message.get("space") or {}
        return (message if isinstance(message, dict) else {}), (space if isinstance(space, dict) else {})
    message = envelope.get("message") or {}
    space = envelope.get("space") or (message.get("space") if isinstance(message, dict) else None) or {}
    return (message if isinstance(message, dict) else {}), (space if isinstance(space, dict) else {})


def _event_type(envelope: dict) -> str:
    """Upper-cased event type. The add-on shape has no top-level ``type`` but a message payload is a MESSAGE."""
    raw = envelope.get("type")
    if raw:
        return str(raw).upper()
    chat = envelope.get("chat")
    if isinstance(chat, dict) and isinstance(chat.get("messagePayload"), dict):
        return EVENT_MESSAGE
    return ""


def _extract_text(message: dict) -> str:
    """The message text. ``argumentText`` has the leading ``@bot`` mention stripped by Google, so it is
    preferred over ``text`` (which still contains it); either way we trim (adapter.py:1835-1836)."""
    value: Any = message.get("argumentText")
    if not value:
        value = message.get("text")
    return str(value or "").strip()
