"""Slack Web API client — chat.postMessage over a static bot-token bearer.

Slack's outbound path needs no token exchange: the ``xoxb-…`` bot token is a long-lived static bearer
credential (no OAuth refresh, no expiry), so unlike Feishu's ``tenant_access_token`` there is no
``_fetch_token`` round trip — :meth:`_fetch_token` just hands the configured token back with a long
TTL, letting :class:`RestChannelClient` cache it and attach ``Authorization: Bearer <token>`` to each
call. The one endpoint an outbound text needs is ``POST /api/chat.postMessage``.

Success is *not* the HTTP status: Slack returns ``200`` with ``{"ok": false, "error": "…"}`` on logical
failures (``channel_not_found``, ``not_in_channel``, ``ratelimited``, …), so we key off the top-level
``ok`` boolean and read the posted message's ``ts`` back as its id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_API_BASE = "https://slack.com"

# Slack's hard cap is 40,000 chars; leave Hermes' margin so a formatted send never trips the limit.
MAX_MESSAGE_LENGTH = 39000

# Static bot tokens never expire; hand RestChannelClient a decade-long TTL so it never re-fetches.
_STATIC_TOKEN_TTL = 315_360_000.0


@dataclass
class SlackConfig:
    """One configured Slack app (a bot) for a single workspace. Credentials, never read from events."""

    bot_token: str                 # xoxb-… — bearer for every Web API call (send, etc.)
    signing_secret: str = ""       # verifies inbound Events API requests (see crypto.py)
    app_token: str = ""            # xapp-… — Socket Mode only; unused on the HTTP-webhook path
    bot_user_id: str = ""          # U… — strips the bot's own <@…> mention from inbound text

    @property
    def base_url(self) -> str:
        return _API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SlackConfig":
        bot_token = env_str("TABVIS_SLACK_BOT_TOKEN", env=env)
        signing_secret = env_str("TABVIS_SLACK_SIGNING_SECRET", env=env)
        if not bot_token or not signing_secret:
            raise RuntimeError(
                "TABVIS_SLACK_BOT_TOKEN and TABVIS_SLACK_SIGNING_SECRET are required to configure the Slack channel"
            )
        return cls(
            bot_token=bot_token,
            signing_secret=signing_secret,
            app_token=env_str("TABVIS_SLACK_APP_TOKEN", env=env),
            bot_user_id=env_str("TABVIS_SLACK_BOT_USER_ID", env=env),
        )


class SlackClient(RestChannelClient):
    def __init__(self, config: SlackConfig, *, client=None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        # No token endpoint: the xoxb- bot token is itself the static bearer, valid until revoked.
        if not self._config.bot_token:
            raise ChannelApiError("TABVIS_SLACK_BOT_TOKEN is not configured")
        return self._config.bot_token, _STATIC_TOKEN_TTL

    async def send_text(self, channel: str, text: str, *, thread_ts: str | None = None) -> str:
        """Post a plain-text message (optionally in a thread); returns the posted message's ``ts``."""
        body: dict = {"channel": channel, "text": text, "mrkdwn": True}
        if thread_ts:
            body["thread_ts"] = thread_ts
        resp = await self.request_json("POST", "/api/chat.postMessage", json_body=body)
        return self._message_ts_or_raise(resp)

    @staticmethod
    def _message_ts_or_raise(resp: dict) -> str:
        # Slack replies 200 even on failure — the truth is the `ok` flag, the error is a string.
        if not resp.get("ok"):
            error = resp.get("error", "unknown")
            raise ChannelApiError(f"slack send error: {error}", code=error, detail=resp)
        return str(resp.get("ts") or "")
