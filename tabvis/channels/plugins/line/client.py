"""LINE Messaging API REST client — reply, push, and the bot's own userId.

Wraps the three LINE Open API endpoints an outbound text (plus self-echo filtering) needs:

* ``POST /v2/bot/message/reply`` — answer an inbound event for *free* with its single-use reply token.
* ``POST /v2/bot/message/push``  — a *metered* send addressed to a chat id (the fallback + cron path).
* ``GET  /v2/bot/info``          — the channel's own ``userId``, fetched once to drop self-echoes.

Unlike the Feishu/WeCom family there is **no token exchange**: LINE's channel access token is long-lived
and used directly as the ``Authorization: Bearer``. :meth:`_fetch_token` therefore just hands the static
token back with a far-future TTL, so :class:`RestChannelClient` attaches it on every call and never
refreshes. Success is HTTP-status-based — LINE returns ``200`` (``{}`` or a ``sentMessages`` array) on
success and ``>= 400`` on failure — so the send/get helpers read ``response.status_code`` directly
rather than a body ``code`` field.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_API_BASE = "https://api.line.me"
_REPLY_PATH = "/v2/bot/message/reply"
_PUSH_PATH = "/v2/bot/message/push"
_BOT_INFO_PATH = "/v2/bot/info"
# api-data.line.me hosts inbound media downloads; out of scope for a text channel.

_STATIC_TOKEN_TTL = 365 * 24 * 3600.0  # long-lived channel token; effectively never refreshed
_PER_BUBBLE_CHARS = 5000  # LINE's hard per-message-bubble limit


@dataclass
class LineConfig:
    """One configured LINE Messaging API channel (a bot). Credentials, never read from payloads."""

    channel_access_token: str
    channel_secret: str            # HMAC key for X-Line-Signature verification
    bot_user_id: str = ""          # this bot's own userId; filters self-echo without a /bot/info call

    @property
    def base_url(self) -> str:
        return _API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LineConfig":
        return cls(
            channel_access_token=env_required("TABVIS_LINE_CHANNEL_ACCESS_TOKEN", env=env),
            channel_secret=env_required("TABVIS_LINE_CHANNEL_SECRET", env=env),
            bot_user_id=env_str("TABVIS_LINE_BOT_USER_ID", env=env),
        )


def _text_message(text: str) -> dict[str, Any]:
    """A LINE text message object, hard-capped to the per-bubble limit."""
    if len(text) > _PER_BUBBLE_CHARS:
        text = text[: _PER_BUBBLE_CHARS - 1] + "…"
    return {"type": "text", "text": text}


def _sent_message_id(resp: dict[str, Any]) -> str:
    """Best-effort extract of the first ``sentMessages`` id; LINE also returns a bare ``{}``."""
    sent = resp.get("sentMessages")
    if isinstance(sent, list) and sent and isinstance(sent[0], dict):
        return str(sent[0].get("id") or "")
    return ""


class LineClient(RestChannelClient):
    def __init__(self, config: LineConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        # LINE has no token endpoint — the configured channel access token is the bearer, long-lived.
        return self._config.channel_access_token, _STATIC_TOKEN_TTL

    async def reply_text(self, reply_token: str, text: str) -> str:
        """Answer an inbound event with its reply token (free); returns the sent message id if any."""
        resp = await self._post(_REPLY_PATH, {"replyToken": reply_token, "messages": [_text_message(text)]})
        return _sent_message_id(resp)

    async def push_text(self, to: str, text: str) -> str:
        """Push a message to a chat id (metered); returns the sent message id if any."""
        resp = await self._post(_PUSH_PATH, {"to": to, "messages": [_text_message(text)]})
        return _sent_message_id(resp)

    async def get_bot_info(self) -> str:
        """This bot's own ``userId`` (for self-echo filtering); ``""`` when unavailable."""
        resp = await self._get(_BOT_INFO_PATH)
        return str(resp.get("userId") or "")

    # --- HTTP: status-based success, static bearer via RestChannelClient's _ensure_token ----------

    async def _post(self, path: str, json_body: Any) -> dict[str, Any]:
        return await self._request("POST", path, json_body=json_body)

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def _request(self, method: str, path: str, *, json_body: Any = None) -> dict[str, Any]:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = path if path.startswith("http") else f"{self._base}{path}"
        response = await self._http().request(method, url, json=json_body, headers=headers)
        if response.status_code >= 400:  # LINE's own success signal is the status code, not a body field
            raise ChannelApiError(
                f"line api error {response.status_code}", code=response.status_code, detail=_safe_json(response)
            )
        return _safe_json(response)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body (e.g. empty 200) becomes an empty envelope
        return {}
    return data if isinstance(data, dict) else {"data": data}
