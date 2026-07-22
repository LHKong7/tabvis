"""Discord REST client — the bot token, plus message send (POST /channels/{id}/messages).

Discord splits its API in two: the live **Gateway websocket** carries inbound events (handled in
:mod:`tabvis.channels.plugins.discord.channel`), while all outbound sends are ordinary REST calls to
``discord.com/api/v10``. Only the REST half needs an HTTP client, so that is all this module is —
outbound text over ``httpx``.

Auth is unlike the Feishu/WeCom family: there is **no token exchange**. A Discord bot token is a
long-lived credential presented verbatim in the ``Authorization: Bot <token>`` header (note the
``Bot`` scheme, not ``Bearer``). We still park it in :class:`RestChannelClient`'s token slot via
:meth:`_fetch_token` with a far-future TTL, but the send path writes the ``Bot`` header itself.
Success is HTTP-status-based — Discord returns ``200``/``201`` with the new message's ``id`` in the
body, and ``>= 400`` on failure — so the send helper reads ``response.status_code`` directly rather
than a body ``code`` field.

Two Discord facts shape the send path:

* **2000-char cap** — Discord rejects a ``content`` longer than 2000 characters, so a long reply is
  split into ≤2000-char chunks sent as separate messages (the first message's id is returned).
* **mention safety** — the body carries ``allowed_mentions: {"parse": ["users"]}`` so echoed or
  model-authored text can never mass-ping ``@everyone``/``@here`` or a role.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from tabvis.channels.plugins._platform.config import env_bool, env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE_LENGTH = 2000  # Discord hard-rejects a longer `content`; chunk before sending.

# The bot token is a permanent credential; hand RestChannelClient a decade-long TTL so it is parked
# once and never re-fetched (there is no rotation endpoint to call).
_STATIC_TOKEN_TTL = 10 * 365 * 24 * 3600.0

# Deny mass-pings on every send: only literal <@user_id> tokens resolve, never @everyone/@here/roles.
_SAFE_MENTIONS = {"parse": ["users"]}


@dataclass
class DiscordConfig:
    """One configured Discord bot. Credentials + admission knobs, never read from event payloads."""

    bot_token: str
    bot_user_id: str = ""          # this bot's own user id; drops self-echo + strips its @mention token
    allow_bots: bool = False       # accept other bots' messages (Hermes' DISCORD_ALLOW_BOTS=all)
    channel_account_id: str = "ca_discord"  # the single account this plugin's read loop serves

    @property
    def base_url(self) -> str:
        return _API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DiscordConfig":
        return cls(
            bot_token=env_required("TABVIS_DISCORD_BOT_TOKEN", env=env),
            bot_user_id=env_str("TABVIS_DISCORD_BOT_USER_ID", env=env),
            # Hermes' DISCORD_ALLOW_BOTS is none/mentions/all; here "all" is the only value that opens
            # the gate to other bots. env_bool also accepts true/1/yes for convenience.
            allow_bots=(
                env_str("TABVIS_DISCORD_ALLOW_BOTS", env=env).strip().lower() == "all"
                or env_bool("TABVIS_DISCORD_ALLOW_BOTS", False, env=env)
            ),
            channel_account_id=env_str("TABVIS_DISCORD_CHANNEL_ACCOUNT_ID", "ca_discord", env=env),
        )


def _chunk(text: str) -> list[str]:
    """Split text into ≤2000-char pieces (Discord's per-message ``content`` cap)."""
    if len(text) <= MAX_MESSAGE_LENGTH:
        return [text]
    return [text[i : i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - a non-JSON body (e.g. an empty 204) becomes an empty envelope
        return {}
    return data if isinstance(data, dict) else {"data": data}


class DiscordClient(RestChannelClient):
    def __init__(self, config: DiscordConfig, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        # Discord has no token endpoint — the configured bot token IS the credential, long-lived.
        return self._config.bot_token, _STATIC_TOKEN_TTL

    async def send_text(self, channel_id: str, text: str) -> str:
        """Send text to a channel (threads are channels too); returns the first message's id.

        Text over 2000 chars is sent as multiple messages; the id of the first is returned as the
        delivery's external message id.
        """
        message_id = ""
        for index, chunk in enumerate(_chunk(text)):
            sent = await self._send_chunk(channel_id, chunk)
            if index == 0:
                message_id = sent
        return message_id

    async def _send_chunk(self, channel_id: str, content: str) -> str:
        body = {"content": content, "allowed_mentions": _SAFE_MENTIONS}
        # `Bot` scheme (not `Bearer`); _ensure_token just parks the static token in the base's cache.
        token = await self._ensure_token()
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        url = f"{self._base}/channels/{channel_id}/messages"
        response = await self._http().request("POST", url, json=body, headers=headers)
        if response.status_code not in {200, 201}:  # Discord's success signal is the status code
            raise ChannelApiError(
                f"discord api error {response.status_code}",
                code=response.status_code,
                detail=_safe_json(response),
            )
        return str(_safe_json(response).get("id") or "")
