"""QQ official bot v2 API client — app access token + group/C2C/channel message send.

QQ authenticates sends with an app access token (``Authorization: QQBot <token>`` — note the ``QQBot``
scheme, not ``Bearer``), obtained from ``bots.qq.com`` and cached until it nears expiry. Messages are
posted to ``api.sgroup.qq.com`` per destination kind. A ``msg_id`` (the id of the message being replied
to) makes the send a *passive* reply, which QQ allows freely; proactive sends are rate-limited.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError

_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
_API_BASE = "https://api.sgroup.qq.com"


@dataclass
class QQConfig:
    app_id: str
    secret: str                        # the AppSecret — also the Ed25519 webhook-signing key
    api_base: str = _API_BASE
    channel_account_id: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "QQConfig":
        return cls(
            app_id=env_required("TABVIS_QQ_APP_ID", env=env),
            secret=env_required("TABVIS_QQ_SECRET", env=env),
            api_base=env_str("TABVIS_QQ_API_BASE", _API_BASE, env=env),
            channel_account_id=env_str("TABVIS_QQ_CHANNEL_ACCOUNT_ID", env=env),
        )


class QQClient:
    def __init__(self, config: QQConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None
        self._token: str | None = None
        self._token_expiry = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def _access_token(self) -> str:
        now = time.monotonic()
        if self._token is None or now >= self._token_expiry:
            response = await self._http().post(
                _TOKEN_URL, json={"appId": self._config.app_id, "clientSecret": self._config.secret}
            )
            data = _json(response)
            token = data.get("access_token")
            if not token:
                raise ChannelApiError(f"qq access token error: {data}", code=response.status_code, detail=data)
            self._token = token
            self._token_expiry = now + max(0.0, float(data.get("expires_in", 7200)) - 60)
        return self._token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {await self._access_token()}", "Content-Type": "application/json"}

    async def _post_message(self, path: str, content: str, msg_id: str | None) -> str:
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:  # a passive reply (free); without it QQ treats the send as rate-limited proactive
            body["msg_id"] = msg_id
        response = await self._http().post(
            f"{self._config.api_base}{path}", json=body, headers=await self._headers()
        )
        data = _json(response)
        if response.status_code not in (200, 201) or data.get("code") not in (None, 0):
            raise ChannelApiError(f"qq send error: {data}", code=response.status_code, detail=data)
        return str(data.get("id") or "")

    async def send_group(self, group_openid: str, content: str, *, msg_id: str | None = None) -> str:
        return await self._post_message(f"/v2/groups/{group_openid}/messages", content, msg_id)

    async def send_c2c(self, user_openid: str, content: str, *, msg_id: str | None = None) -> str:
        return await self._post_message(f"/v2/users/{user_openid}/messages", content, msg_id)

    async def send_channel(self, channel_id: str, content: str, *, msg_id: str | None = None) -> str:
        return await self._post_message(f"/channels/{channel_id}/messages", content, msg_id)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


def _json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {"data": data}
