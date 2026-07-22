"""SlackChannel — a Slack IM channel plugin (design §4.2, §4.8).

Implements the :class:`~tabvis.channels.core.contract.ChannelPlugin` contract for Slack's Events API
``message`` / ``app_mention`` events. Slack's inbound verification is its own scheme — an HMAC over a
``v0:{timestamp}:{body}`` base string plus a replay window, and a ``url_verification`` challenge on the
first POST — rather than the framework's plain HMAC over the raw body, so this plugin declares
``signed_webhooks=False`` and does that verification itself in :meth:`handle_webhook` before handing a
clean :class:`RawInbound` to the gateway's inbound pipeline. Outbound text is sent through
``chat.postMessage``, addressed to the channel the run's conversation is bound to.

The Hermes reference adapter runs on Socket Mode (a persistent WebSocket via ``slack-bolt``); this
plugin takes the equivalent Events API HTTP-webhook path so it stays on tabvis' stdlib+httpx footprint
with no extra SDK. The two carry the *same* event JSON — Socket Mode just delivers it over a socket
instead of a signed POST — so normalize/deliver are identical; only the transport-level trust differs
(a signed request here, a held app token there).

Wiring sketch (a transport / HTTP route drives it)::

    slack = SlackChannel.from_env()
    gateway.register_plugin(slack)
    gateway.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
    await gateway.start_plugin("slack")

    # in the POST handler for the Slack Events request URL:
    result = slack.handle_webhook(request.headers, raw_body)
    if result.challenge is not None:      # url_verification handshake
        return json({"challenge": result.challenge})
    if result.rejected:                   # bad signature / stale timestamp / body
        return status(401)
    await gateway.receive_webhook("ca_slack", result.raw)   # dedupe -> bind -> event -> Run
    return status(200)                    # always answer fast; Slack retries non-2xx (same ts)
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
from tabvis.channels.plugins.slack.client import SlackClient, SlackConfig
from tabvis.channels.plugins.slack.crypto import verify_signature

PLUGIN_ID = "slack"

# Slack fires both of these for an @mention (sharing one ``ts``); the dedupe below collapses the pair.
_MESSAGE_EVENT_TYPES = {"message", "app_mention"}
# Edits and deletes replay old text over the same channel — never a new turn.
_IGNORED_SUBTYPES = {"message_changed", "message_deleted"}


@dataclass
class SlackWebhookResult:
    """What decoding a raw Slack HTTP webhook tells the transport to do next."""

    challenge: str | None = None   # echo ``{"challenge": ...}`` with HTTP 200
    raw: RawInbound | None = None  # hand to ChannelGateway.receive_webhook
    rejected: bool = False         # respond 401/403; nothing was ingested
    reason: str | None = None


class SlackChannel:
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # Slack's scheme is custom; verified in handle_webhook, not the gateway
    )

    def __init__(self, config: SlackConfig, *, client: SlackClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else SlackClient(config)
        self._services: ChannelServices | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SlackChannel":
        return cls(SlackConfig.from_env(env))

    async def start(self, services: ChannelServices) -> None:
        self._services = services

    async def stop(self) -> None:
        self._services = None
        await self._client.aclose()

    async def health(self) -> ChannelHealth:
        return ChannelHealth(status="ready" if self._services is not None else "stopped")

    # --- inbound webhook decoding (transport-facing) -------------------------------------------

    def handle_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> SlackWebhookResult:
        """Verify a raw Slack Events request and say what the transport should do.

        Order mirrors Slack's own contract: parse → signature (Slack signs *every* request, the
        challenge included, so this comes first) → ``url_verification`` challenge → build the inbound.
        Any failure returns ``rejected=True`` and nothing is ingested.
        """
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return SlackWebhookResult(rejected=True, reason="invalid JSON body")
        if not isinstance(payload, dict):
            return SlackWebhookResult(rejected=True, reason="unexpected payload shape")

        # Signature — enforced whenever a signing secret is configured (the real-world case).
        if self._config.signing_secret:
            ok = verify_signature(
                self._config.signing_secret,
                lower.get("x-slack-request-timestamp", ""),
                raw_body,
                lower.get("x-slack-signature"),
            )
            if not ok:
                return SlackWebhookResult(rejected=True, reason="signature mismatch")

        # URL verification handshake — echo the challenge (after the signature check above).
        if payload.get("type") == "url_verification":
            return SlackWebhookResult(challenge=str(payload.get("challenge", "")))

        event = payload.get("event") or {}
        raw = RawInbound(
            # ``ts`` is THE dedupe key: retries and the message/app_mention double all share it, so the
            # gateway's external_event_id ledger suppresses both (design §4.5). Fall back to event_id.
            external_event_id=str(event.get("ts") or payload.get("event_id") or ""),
            external_conversation_id=str(event.get("channel") or ""),
            external_account_ref=str(payload.get("team_id") or payload.get("api_app_id") or ""),
            payload=payload,
        )
        return SlackWebhookResult(raw=raw)

    # --- ChannelPlugin protocol ----------------------------------------------------------------

    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]:
        payload = inbound.payload or {}
        if payload.get("type") != "event_callback":
            return []  # url_verification and non-event envelopes produce no inbound message
        event = payload.get("event") or {}
        if event.get("type") not in _MESSAGE_EVENT_TYPES:
            return []  # reactions, joins, etc. aren't message turns
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return []  # never react to our own or another bot's messages
        if event.get("subtype") in _IGNORED_SUBTYPES:
            return []  # edits/deletes aren't new turns
        text = _clean_text(str(event.get("text") or ""), self._config.bot_user_id)
        if not text:
            return []
        return [
            InboundMessage(
                external_event_id=str(event.get("ts") or inbound.external_event_id),
                external_conversation_id=str(event.get("channel") or inbound.external_conversation_id),
                external_account_ref=inbound.external_account_ref,
                text=text,
                external_user_id=event.get("user"),
            )
        ]

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        channel = self._resolve_channel_id(outbound)
        if not channel:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external channel id for conversation"
            )
        try:
            message_ts = await self._client.send_text(channel, outbound.text)
        except Exception as exc:  # noqa: BLE001 - a send failure is reported as a receipt, not raised
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=message_ts)

    async def acknowledge(self, external_event_id: str) -> None:
        return None

    def _resolve_channel_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the Slack channel id is the binding's external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


# --- inbound text extraction -------------------------------------------------------------------


def _clean_text(text: str, bot_user_id: str) -> str:
    """Strip the bot's own ``<@U…>`` mention so channel prompts read the same as a DM's.

    Slack renders mentions as literal ``<@U…>`` tokens; in a channel the trigger is the bot's mention,
    which shouldn't leak into the run prompt. Other users' mentions are left as-is (we have no name
    lookup on this path). Mirrors Feishu's ``_clean_mentions`` intent.
    """
    if not text:
        return ""
    result = text.replace(f"<@{bot_user_id}>", "") if bot_user_id else text
    return result.strip()
