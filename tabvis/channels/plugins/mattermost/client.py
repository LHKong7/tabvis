"""Mattermost REST client — plain-text post send over the ``/api/v4`` API.

Mattermost's outbound is a single JSON POST: ``POST {url}/api/v4/posts`` with a
``{"channel_id", "message"}`` body, authenticated by a **static** bot-account or personal-access
token as ``Authorization: Bearer <token>``. Like LINE (and unlike Feishu/WeCom) there is *no token
exchange* — the configured token already *is* the bearer — so :meth:`_fetch_token` hands it straight
back with a far-future TTL and :class:`RestChannelClient` never refreshes it.

Success is HTTP-status-based, and this matters: on ``>= 400`` Mattermost returns an error object that
*also* carries an ``id`` field (an ``api.*`` error code), so a naive "body has an ``id``" check would
read a rejection as a success. The send helper therefore inspects ``response.status_code`` first and
only then trusts the created post's ``id``.

Base URLs are all derived from ``MATTERMOST_URL`` — there is no separate host var:

* REST base   : ``{url}/api/v4/…``
* WebSocket   : ``{url}`` with ``http`` → ``ws`` swapped, + ``/api/v4/websocket`` (the live inbound).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_POSTS_PATH = "/api/v4/posts"
_ME_PATH = "/api/v4/users/me"

_STATIC_TOKEN_TTL = 365 * 24 * 3600.0  # long-lived bot/PAT token; effectively never refreshed
_MAX_POST_LENGTH = 16383  # Mattermost's server-side hard limit; a longer message is rejected


@dataclass
class MattermostConfig:
    """One configured Mattermost bot. Credentials + addressing, never read from event payloads."""

    url: str                              # server base, e.g. https://mm.example.com (trailing / stripped)
    token: str                            # bot-account or personal-access token — the bearer, used as-is
    bot_user_id: str = ""                 # this bot's own user id; drops self-echo without a /users/me call
    bot_username: str = ""                # this bot's @-name; used to strip the mention from channel text
    channel_account_id: str = "ca_mattermost"  # the single tabvis account this plugin instance serves

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def websocket_url(self) -> str:
        # The WS endpoint is the REST base with the scheme swapped: https→wss, http→ws.
        return re.sub(r"^http", "ws", self.base_url) + "/api/v4/websocket"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MattermostConfig":
        return cls(
            url=env_required("TABVIS_MATTERMOST_URL", env=env).rstrip("/"),
            token=env_required("TABVIS_MATTERMOST_TOKEN", env=env),
            bot_user_id=env_str("TABVIS_MATTERMOST_BOT_USER_ID", env=env),
            bot_username=env_str("TABVIS_MATTERMOST_BOT_USERNAME", env=env),
            channel_account_id=env_str(
                "TABVIS_MATTERMOST_CHANNEL_ACCOUNT_ID", "ca_mattermost", env=env
            ),
        )


class MattermostClient(RestChannelClient):
    def __init__(self, config: MattermostConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        # No token endpoint — the configured token is the bearer. Hand it back with a far-future TTL.
        return self._config.token, _STATIC_TOKEN_TTL

    async def send_text(self, channel_id: str, text: str, *, root_id: str | None = None) -> str:
        """Create a post in ``channel_id``; returns the new post id.

        ``root_id`` threads the reply under a root post (unused by default — the plugin sends flat).
        """
        if len(text) > _MAX_POST_LENGTH:
            text = text[: _MAX_POST_LENGTH - 1] + "…"
        body: dict[str, Any] = {"channel_id": channel_id, "message": text}
        if root_id:
            body["root_id"] = root_id
        resp = await self._request("POST", _POSTS_PATH, json_body=body)
        return str(resp.get("id") or "")

    async def get_me(self) -> tuple[str, str]:
        """This bot's ``(user_id, username)`` — fetched once at connect for self-echo/mention filtering."""
        resp = await self._request("GET", _ME_PATH)
        return str(resp.get("id") or ""), str(resp.get("username") or "")

    # --- HTTP: static bearer + status-based success -----------------------------------------------

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = path if path.startswith("http") else f"{self._base}{path}"
        response = await self._http().request(method, url, json=json_body, headers=headers)
        # Mattermost signals failure by status, not a body field — and error bodies still carry `id`.
        if response.status_code >= 400:
            raise ChannelApiError(
                f"mattermost api error {response.status_code}",
                code=response.status_code,
                detail=_safe_json(response),
            )
        return _safe_json(response)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body (e.g. empty 200) becomes an empty envelope
        return {}
    return data if isinstance(data, dict) else {"data": data}
