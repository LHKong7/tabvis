"""Feishu / Lark REST client — tenant access token, plus message send and reply.

Wraps the two Feishu Open Platform endpoints an outbound text needs:

* ``POST /open-apis/auth/v3/tenant_access_token/internal`` — exchange app id/secret for a bearer
  token (cached + refreshed by :class:`RestChannelClient`).
* ``POST /open-apis/im/v1/messages`` (and ``/messages/{id}/reply``) — send a message.

The base host is ``open.feishu.cn`` (China) or ``open.larksuite.com`` (International ``lark`` domain).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_OPEN_BASE = {"feishu": "https://open.feishu.cn", "lark": "https://open.larksuite.com"}


@dataclass
class FeishuConfig:
    """One configured Feishu/Lark app (a bot). Credentials, never read from event payloads."""

    app_id: str
    app_secret: str
    encrypt_key: str = ""          # AES key for encrypted events + the signature (optional)
    verification_token: str = ""   # checked against header.token (optional)
    domain: str = "feishu"         # "feishu" (China) | "lark" (International)

    @property
    def base_url(self) -> str:
        return _OPEN_BASE["lark"] if self.domain == "lark" else _OPEN_BASE["feishu"]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeishuConfig":
        app_id = env_str("TABVIS_FEISHU_APP_ID", env=env)
        app_secret = env_str("TABVIS_FEISHU_APP_SECRET", env=env)
        if not app_id or not app_secret:
            raise RuntimeError(
                "TABVIS_FEISHU_APP_ID and TABVIS_FEISHU_APP_SECRET are required to configure the Feishu channel"
            )
        domain = (env_str("TABVIS_FEISHU_DOMAIN", "feishu", env=env)).strip().lower()
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            encrypt_key=env_str("TABVIS_FEISHU_ENCRYPT_KEY", env=env),
            verification_token=env_str("TABVIS_FEISHU_VERIFICATION_TOKEN", env=env),
            domain="lark" if domain == "lark" else "feishu",
        )


class FeishuClient(RestChannelClient):
    def __init__(self, config: FeishuConfig, *, client=None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        resp = await self.request_json(
            "POST",
            "/open-apis/auth/v3/tenant_access_token/internal",
            json_body={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
            auth=False,
        )
        if resp.get("code") != 0 or not resp.get("tenant_access_token"):
            raise ChannelApiError(
                f"feishu tenant_access_token error: {resp.get('msg')}", code=resp.get("code"), detail=resp
            )
        return resp["tenant_access_token"], float(resp.get("expire", 7200))

    async def send_text(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id") -> str:
        """Send a plain-text message; returns the Feishu ``message_id``."""
        resp = await self.request_json(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_id_or_raise(resp)

    async def reply_text(self, message_id: str, text: str, *, in_thread: bool = False) -> str:
        """Reply to a specific message (optionally in its thread); returns the new ``message_id``."""
        resp = await self.request_json(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
                "reply_in_thread": in_thread,
                "uuid": str(uuid.uuid4()),
            },
        )
        return self._message_id_or_raise(resp)

    @staticmethod
    def _message_id_or_raise(resp: dict) -> str:
        if resp.get("code") != 0:
            raise ChannelApiError(f"feishu send error: {resp.get('msg')}", code=resp.get("code"), detail=resp)
        return ((resp.get("data") or {}).get("message_id")) or ""
