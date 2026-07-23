"""WhatsApp Cloud (Meta Graph) REST client — the permanent access token, plus message send.

Wraps the single Graph endpoint an outbound text needs:

* ``POST /{api_version}/{phone_number_id}/messages`` — send a message as the business number.

Unlike Feishu (which exchanges app id/secret for a short-lived ``tenant_access_token``), WhatsApp Cloud
authenticates with a **permanent System-User access token** supplied via env: there is no OAuth
exchange, no refresh, no TTL. We still route it through :class:`RestChannelClient`'s token slot — by
returning it from :meth:`_fetch_token` with a very long TTL — so ``request_json(auth=True)`` sends it
as the ``Authorization: Bearer`` header exactly like every other platform client, and it is fetched
once and never rotated.

The base host is ``graph.facebook.com``; the sender identity is the ``phone_number_id`` path segment
(not the phone number itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

GRAPH_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v20.0"

# The System-User token is permanent; hand RestChannelClient a decade-long TTL so it never re-fetches.
_STATIC_TOKEN_TTL = 10 * 365 * 24 * 3600.0


@dataclass
class WhatsAppConfig:
    """One configured WhatsApp Cloud number (a bot). Credentials, never read from event payloads."""

    phone_number_id: str            # Graph URL path component / sender identity (NOT the phone number)
    access_token: str               # System-User *permanent* bearer token — no refresh, no OAuth
    app_secret: str = ""            # HMAC key for X-Hub-Signature-256 (required to accept inbound POSTs)
    verify_token: str = ""          # hub.verify_token shared secret for the GET subscription handshake
    api_version: str = DEFAULT_API_VERSION

    @property
    def base_url(self) -> str:
        return GRAPH_API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WhatsAppConfig":
        phone_number_id = env_str("TABVIS_WHATSAPP_PHONE_NUMBER_ID", env=env)
        access_token = env_str("TABVIS_WHATSAPP_ACCESS_TOKEN", env=env)
        if not phone_number_id or not access_token:
            raise RuntimeError(
                "TABVIS_WHATSAPP_PHONE_NUMBER_ID and TABVIS_WHATSAPP_ACCESS_TOKEN are required to "
                "configure the WhatsApp channel"
            )
        return cls(
            phone_number_id=phone_number_id,
            access_token=access_token,
            app_secret=env_str("TABVIS_WHATSAPP_APP_SECRET", env=env),
            verify_token=env_str("TABVIS_WHATSAPP_VERIFY_TOKEN", env=env),
            api_version=env_str("TABVIS_WHATSAPP_API_VERSION", DEFAULT_API_VERSION, env=env),
        )


class WhatsAppClient(RestChannelClient):
    def __init__(self, config: WhatsAppConfig, *, client=None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        # No token endpoint exists: the permanent env token IS the bearer token. Returning it with a
        # long TTL parks it in the RestChannelClient cache so auth'd requests carry it, unchanged.
        return self._config.access_token, _STATIC_TOKEN_TTL

    async def send_text(
        self, to: str, text: str, *, reply_to: str | None = None, preview_url: bool = True
    ) -> str:
        """Send a plain-text message to a wa_id; returns the Graph ``wamid`` of the sent message."""
        body: dict = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,  # recipient wa_id: country code + number, digits only, no '+' and no '@' suffix
            "type": "text",
            "text": {"body": text, "preview_url": preview_url},
        }
        if reply_to:  # quote the inbound message (Meta gives us only the wamid to quote, never the text)
            body["context"] = {"message_id": reply_to}
        resp = await self.request_json(
            "POST",
            f"/{self._config.api_version}/{self._config.phone_number_id}/messages",
            json_body=body,
        )
        return self._message_id_or_raise(resp)

    @staticmethod
    def _message_id_or_raise(resp: dict) -> str:
        """Extract the sent ``wamid`` from a Graph success, or raise the Graph error shape.

        Success is ``{"messages": [{"id": "wamid..."}]}``; a failure is
        ``{"error": {"message","type","code","fbtrace_id"}}``.
        """
        error = resp.get("error")
        if error:
            raise ChannelApiError(
                f"graph error {error.get('code')}: {error.get('message')}",
                code=error.get("code"),
                detail=resp,
            )
        messages = resp.get("messages") or []
        if messages and isinstance(messages[0], dict):
            return str(messages[0].get("id") or "")
        return ""
