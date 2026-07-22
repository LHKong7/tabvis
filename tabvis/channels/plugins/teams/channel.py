"""TeamsChannel — a Microsoft Teams / Bot Framework channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for Teams' Bot Framework
message activities. Teams is the odd one out among the channel plugins: inbound is **not** an
HMAC-signed webhook with a challenge handshake, it is a signed **JWT Bearer** token (RS256 via JWKS,
see :mod:`tabvis.channels.plugins.teams.crypto`). So — like Feishu — this plugin declares
``signed_webhooks=False`` and does the platform's own verification itself in :meth:`handle_webhook`
before handing a clean :class:`RawInbound` to the gateway pipeline. Outbound text is an OAuth2
client-credentials token plus a REST POST to the conversation's Bot Framework service.

The Bot Framework JWKS (the signing keys) is normally fetched + cached from the well-known metadata
endpoint. :meth:`refresh_signing_keys` does that fetch (a two-hop metadata → jwks_uri call over the
REST client's ``httpx``); the synchronous :meth:`handle_webhook` verifies against the *already loaded*
keys so it never blocks on the network. Wire ``refresh_signing_keys`` into boot / periodic refresh.

Wiring sketch (a transport / HTTP route drives it)::

    teams = TeamsChannel.from_env()
    await teams.refresh_signing_keys()        # load the Bot Framework JWKS once at boot
    gateway.register_plugin(teams)
    gateway.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
    await gateway.start_plugin("teams")

    # in the POST /api/messages handler for the Bot Framework endpoint:
    result = teams.handle_webhook(request.headers, raw_body)
    if result.rejected:                       # JWT missing / bad signature / bad claims
        return status(401)
    await gateway.receive_webhook("ca_teams", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)                        # Bot Framework wants a 200 with an (optional) empty body
"""

from __future__ import annotations

import json
import re
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
from tabvis.channels.plugins.teams import crypto
from tabvis.channels.plugins.teams.client import TeamsClient, TeamsConfig

PLUGIN_ID = "teams"
# The top-level ``channelId`` on every Teams activity — the platform tag, not a Teams *channel*.
TEAMS_CHANNEL_ID = "msteams"
# Teams prepends the bot @mention into the text as an <at>…</at> span; strip it to recover the prompt.
_MENTION_RE = re.compile(r"<at>[^<]*</at>\s*")


@dataclass
class TeamsWebhookResult:
    """What decoding a raw Bot Framework HTTP request tells the transport to do next.

    ``challenge`` is kept for structural parity with the other channel plugins but is always ``None``:
    Bot Framework has no ``url_verification`` challenge handshake — the JWT *is* the whole verification.
    """

    challenge: str | None = None   # Bot Framework has no challenge handshake; always None here
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class TeamsChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # Bot Framework auth is a JWT, not the framework's HMAC gate
    )

    def __init__(
        self,
        config: TeamsConfig,
        *,
        client: TeamsClient | None = None,
        signing_keys: crypto.SigningKeyStore | None = None,
    ) -> None:
        self._config = config
        self._client = client if client is not None else TeamsClient(config)
        self._services: ChannelServices | None = None
        self._signing_keys = signing_keys if signing_keys is not None else crypto.SigningKeyStore()
        # Per-conversation serviceUrl seen on inbound activities — replies prefer it over the default.
        self._service_urls: dict[str, str] = {}

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TeamsChannel":
        return cls(TeamsConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- signing keys (Bot Framework JWKS) -----------------------------------------------------

    def load_signing_keys(self, jwks_document: Mapping[str, Any]) -> None:
        """Merge a JWKS document into the verifier's key store (tests + the refresh path use this)."""
        self._signing_keys.load_jwks(jwks_document)

    async def refresh_signing_keys(self) -> None:
        """Fetch the Bot Framework OpenID metadata → JWKS and load the signing keys.

        The channel → bot metadata document points at a ``jwks_uri``; fetch that, then the keys. Called
        at boot and periodically (keys rotate); :meth:`handle_webhook` only ever reads the cached set.
        """
        metadata = await self._client.request_json("GET", crypto.OPENID_METADATA_URL, auth=False)
        jwks_uri = metadata.get("jwks_uri")
        if not jwks_uri:
            return
        self._signing_keys.load_jwks(await self._client.request_json("GET", jwks_uri, auth=False))

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> TeamsWebhookResult:
        """Verify the Bot Framework JWT and say what the transport should do.

        Order mirrors the SDK: parse the activity JSON → read ``Authorization: Bearer`` → RS256 + claim
        validation (audience/issuer/expiry/serviceUrl) against the loaded JWKS. Any failure returns
        ``rejected=True`` and nothing is ingested. There is no challenge branch — Teams has no handshake.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return TeamsWebhookResult(rejected=True, reason="invalid JSON body")
        if not isinstance(payload, dict):
            return TeamsWebhookResult(rejected=True, reason="unexpected payload shape")

        auth = lower.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not token:
            return TeamsWebhookResult(rejected=True, reason="missing bearer token")

        try:
            crypto.validate_bearer(
                token,
                key_store=self._signing_keys,
                audience=self._config.client_id,
                service_url=str(payload.get("serviceUrl") or "") or None,
            )
        except crypto.JwtError as exc:
            return TeamsWebhookResult(rejected=True, reason=f"jwt validation failed: {exc}")

        conversation = payload.get("conversation") or {}
        recipient = payload.get("recipient") or {}
        raw = RawInbound(
            external_event_id=str(payload.get("id") or ""),
            external_conversation_id=str(conversation.get("id") or ""),
            external_account_ref=str(recipient.get("id") or self._config.client_id),
            payload=payload,
        )
        return TeamsWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        if payload.get("type") != "message":
            return []  # typing, conversationUpdate, invoke, … produce no inbound text message
        sender = payload.get("from") or {}
        sender_id = str(sender.get("id") or "")
        if self._is_own_message(sender_id):
            return []  # never react to our own bot's messages (echo-loop guard)
        text = _extract_text(payload)
        if not text:
            return []
        conversation = payload.get("conversation") or {}
        conversation_id = str(conversation.get("id") or inbound.external_conversation_id)
        # Remember the serviceUrl this conversation lives on so a later reply targets the right host.
        service_url = payload.get("serviceUrl")
        if conversation_id and service_url:
            self._service_urls[conversation_id] = str(service_url)
        # Prefer the stable AAD object id; fall back to the (channel-prefixed) Bot Framework id.
        external_user_id = sender.get("aadObjectId") or sender.get("id")
        return [
            InboundMessage(
                external_event_id=str(payload.get("id") or inbound.external_event_id),
                external_conversation_id=conversation_id,
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
            message_id = await self._client.send_text(
                conversation_id, outbound.text, service_url=self._service_urls.get(conversation_id)
            )
        except Exception as exc:  # noqa: BLE001 - a send failure is a receipt, not a raised error
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_id)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_conversation_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the Teams conversation id is the binding's.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None

    def _is_own_message(self, sender_id: str) -> bool:
        """True when the activity is from our own bot.

        In raw Bot Framework the bot's ``from.id`` is channel-prefixed (``28:<client_id>``) while a user's
        is ``29:<id>``; compare against the client id both directly and with the ``NN:`` prefix stripped.
        """
        if not sender_id:
            return False
        bot_id = self._config.client_id
        return sender_id == bot_id or sender_id.split(":", 1)[-1] == bot_id


# --- inbound text extraction -------------------------------------------------------------------


def _extract_text(activity: dict) -> str:
    """Recover the plain prompt from an activity: the ``text`` with the bot @mention span stripped."""
    text = str(activity.get("text") or "")
    if "<at>" in text:
        text = _MENTION_RE.sub("", text)
    return text.strip()
