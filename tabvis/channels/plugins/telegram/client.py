"""Telegram Bot API client — ``getMe`` / ``getUpdates`` long-poll / ``sendMessage``.

Telegram's bot transport is plain HTTPS: methods are ``https://api.telegram.org/bot<TOKEN>/<method>``
with the token in the URL path (no header auth, no token exchange), so this is a thin ``httpx`` wrapper
rather than a :class:`RestChannelClient` subclass. Hermes drives Telegram through the
``python-telegram-bot`` SDK, but that SDK is only a convenience wrapper over exactly these calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import httpx

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError

_DEFAULT_BASE = "https://api.telegram.org"


def normalize_chat_id(value: Any) -> Any:
    """Telegram chat ids are integers (DMs positive, groups negative) or ``@username`` strings.

    Send a numeric id as an ``int`` (a numeric *string* is rejected by the Bot API) and a handle as a
    trimmed string — never ``int()`` a value blindly (that breaks ``@channel`` handles).
    """
    text = str(value).strip()
    if text and text.lstrip("-").isdigit():
        return int(text)
    return text


@dataclass
class TelegramConfig:
    bot_token: str
    allowed_users: tuple[str, ...] = field(default_factory=tuple)
    base_url: str = _DEFAULT_BASE
    channel_account_id: str = ""
    poll_timeout: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "TelegramConfig":
        token = env_required("TABVIS_TELEGRAM_BOT_TOKEN", env=env)
        allowed = env_str("TABVIS_TELEGRAM_ALLOWED_USERS", env=env)
        return cls(
            bot_token=token,
            allowed_users=tuple(u.strip() for u in allowed.split(",") if u.strip()),
            base_url=env_str("TABVIS_TELEGRAM_BASE_URL", _DEFAULT_BASE, env=env),
            channel_account_id=env_str("TABVIS_TELEGRAM_CHANNEL_ACCOUNT_ID", env=env),
        )


class TelegramClient:
    def __init__(self, config: TelegramConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _url(self, method: str) -> str:
        return f"{self._config.base_url}/bot{self._config.bot_token}/{method}"

    async def _call(self, method: str, body: dict | None = None, *, timeout: float | None = None) -> Any:
        response = await self._http().post(self._url(method), json=body or {}, timeout=timeout)
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            data = {}
        if not isinstance(data, dict) or not data.get("ok"):
            raise ChannelApiError(
                f"telegram {method} error: {data.get('description') if isinstance(data, dict) else 'bad response'}",
                code=data.get("error_code") if isinstance(data, dict) else None,
                detail=data,
            )
        return data.get("result")

    async def get_me(self) -> dict:
        return await self._call("getMe") or {}

    async def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict]:
        body: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message", "channel_post"],
        }
        if offset is not None:
            body["offset"] = offset
        # Read timeout must outlast the server's long-poll hold, else every poll aborts mid-wait.
        return await self._call("getUpdates", body, timeout=timeout + 15) or []

    async def send_message(self, chat_id: Any, text: str, *, reply_to_message_id: int | None = None) -> str:
        body: dict[str, Any] = {
            "chat_id": normalize_chat_id(chat_id),
            "text": text,
            "parse_mode": None,  # plain text — never risk a MarkdownV2 parse error on arbitrary output
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id
        result = await self._call("sendMessage", body)
        return str((result or {}).get("message_id", ""))

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
