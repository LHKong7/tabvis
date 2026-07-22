"""Microsoft Teams / Bot Framework REST client — OAuth2 token, plus outbound message send.

Outbound Teams is a plain OAuth2 client-credentials grant followed by a REST POST — no SDK required
(this is the ``_standalone_send`` path in the Hermes adapter, reproduced here on ``httpx``):

* ``POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`` — exchange the Azure AD app
  id/secret for a bearer token, scope ``https://api.botframework.com/.default`` (cached + refreshed by
  :class:`RestChannelClient`). This endpoint speaks ``application/x-www-form-urlencoded``, not JSON, so
  the token fetch posts a form rather than going through ``request_json``.
* ``POST {service_url}v3/conversations/{conversation_id}/activities`` — send a message activity. The
  ``service_url`` is per-conversation (it arrives on each inbound activity) and is host-allowlisted to
  block SSRF / token exfiltration via a tampered value; the conversation id is charset-validated so it
  cannot path-traverse out of the URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlparse

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

# Bot Framework default service host for the global Teams endpoint (note the trailing slash so callers
# append ``v3/conversations/...`` cleanly). Regional/gov tenants override via TABVIS_TEAMS_SERVICE_URL.
_DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/teams/"

# Only these Bot Framework hosts may receive a freshly minted bearer token — an allowlist that blocks
# SSRF / token exfiltration through a tampered serviceUrl (global cloud + US government cloud).
_ALLOWED_SERVICE_HOSTS = frozenset(
    {"smba.trafficmanager.net", "smba.infra.gov.teams.microsoft.us"}
)

# Bot Framework conversation ids combine digits, ':' , '@', '.', '-', '_' (e.g. "19:abc@thread.v2");
# reject anything else so a hostile id cannot escape /v3/conversations/<id>/activities.
_CONV_ID_RE = re.compile(r"^[A-Za-z0-9:@\-_.]+$")
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9\-.]+$")

_TOKEN_URL = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
_BOT_FRAMEWORK_SCOPE = "https://api.botframework.com/.default"


def validate_service_url(raw: str) -> str | None:
    """Return the normalized (trailing-slash) service URL, or ``None`` if it is not on the allowlist."""
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return None
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_SERVICE_HOSTS:
        return None
    return raw if raw.endswith("/") else raw + "/"


@dataclass
class TeamsConfig:
    """One configured Teams bot (an Azure AD / Bot Framework app). Credentials, never from a payload."""

    client_id: str          # Azure AD app id; also the bot's own identity (used for self-filtering)
    client_secret: str      # Azure AD app client secret
    tenant_id: str          # Azure AD tenant id (goes into the token URL)
    service_url: str = _DEFAULT_SERVICE_URL  # Bot Framework service host override

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TeamsConfig":
        client_id = env_required("TABVIS_TEAMS_CLIENT_ID", env=env)
        client_secret = env_required("TABVIS_TEAMS_CLIENT_SECRET", env=env)
        tenant_id = env_required("TABVIS_TEAMS_TENANT_ID", env=env)
        service_url = env_str("TABVIS_TEAMS_SERVICE_URL", _DEFAULT_SERVICE_URL, env=env).strip()
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=tenant_id,
            service_url=service_url or _DEFAULT_SERVICE_URL,
        )


class TeamsClient(RestChannelClient):
    def __init__(self, config: TeamsConfig, *, client=None) -> None:
        # base_url is only a fallback; each send builds an absolute activities URL from the serviceUrl.
        super().__init__(config.service_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        """OAuth2 client-credentials against Azure AD. Returns ``(access_token, expires_in)``.

        The token endpoint requires ``application/x-www-form-urlencoded`` (not JSON), so this posts a
        form directly on the underlying client rather than via ``request_json``.
        """
        tenant = self._config.tenant_id
        if not _TENANT_ID_RE.match(tenant):
            raise ChannelApiError("teams tenant id contains characters outside the expected set", detail=tenant)
        response = await self._http().post(
            _TOKEN_URL.format(tenant_id=tenant),
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "scope": _BOT_FRAMEWORK_SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            payload = {}
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if response.status_code >= 400 or not token:
            raise ChannelApiError(
                f"teams token request failed ({response.status_code})",
                code=response.status_code,
                detail=payload,
            )
        # AAD returns expires_in seconds (typically 3600); the base caches under a refresh margin.
        return token, float(payload.get("expires_in", 3600))

    async def send_text(
        self, conversation_id: str, text: str, *, service_url: str | None = None, text_format: str = "markdown"
    ) -> str:
        """POST a text message activity to a conversation; returns the Bot Framework activity id.

        ``service_url`` is the per-conversation Bot Framework host (from the inbound activity); it falls
        back to the configured default. Both it and ``conversation_id`` are validated before use.
        """
        base = validate_service_url(service_url or self._config.service_url)
        if base is None:
            raise ChannelApiError(
                f"teams service url is not on the Bot Framework allowlist {sorted(_ALLOWED_SERVICE_HOSTS)}"
            )
        if not conversation_id or not _CONV_ID_RE.match(conversation_id):
            raise ChannelApiError("teams conversation id contains characters outside the Bot Framework set")
        url = f"{base}v3/conversations/{conversation_id}/activities"
        resp = await self.request_json(
            "POST", url, json_body={"type": "message", "text": text, "textFormat": text_format}
        )
        error = resp.get("error")
        if error:
            detail = error if isinstance(error, dict) else {"message": error}
            raise ChannelApiError(
                f"teams send error: {detail.get('message')}", code=detail.get("code"), detail=resp
            )
        message_id = resp.get("id")
        if not message_id:
            raise ChannelApiError("teams send: response missing activity id", detail=resp)
        return str(message_id)
