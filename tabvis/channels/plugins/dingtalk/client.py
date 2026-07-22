"""钉钉 DingTalk REST client — OpenAPI access token, plus robot text send.

Wraps the two DingTalk v1.0 OpenAPI endpoints an outbound text needs:

* ``POST /v1.0/oauth2/accessToken`` — exchange the app key/secret for a short-lived access token
  (cached + refreshed by :class:`RestChannelClient`).
* ``POST /v1.0/robot/groupMessages/send`` — send a message into a conversation the bot is in,
  addressed by its ``openConversationId``.

Unlike Feishu, DingTalk's v1.0 OpenAPI does **not** use ``Authorization: Bearer`` — the token rides
in the ``x-acs-dingtalk-access-token`` header. So the send path fetches the token explicitly
(reusing the base class's cache via :meth:`_ensure_token`) and passes it as that header rather than
letting the base attach a bearer. The base host is ``api.dingtalk.com`` (the new v1.0 OpenAPI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_API_BASE = "https://api.dingtalk.com"
_ACCESS_TOKEN_HEADER = "x-acs-dingtalk-access-token"


@dataclass
class DingTalkConfig:
    """One configured DingTalk app (a bot). Credentials, never read from event payloads.

    ``client_secret`` doubles as the HMAC key that verifies the outgoing-robot callback signature
    (see :mod:`tabvis.channels.plugins.dingtalk.crypto`) — DingTalk signs with the app secret, so
    there is no separate signing token to configure.
    """

    client_id: str      # DingTalk app key ("Client ID")
    client_secret: str  # DingTalk app secret ("Client Secret") — also the callback signing key
    robot_code: str = ""  # OpenAPI robotCode; defaults to client_id when unset

    @property
    def base_url(self) -> str:
        return _API_BASE

    @property
    def effective_robot_code(self) -> str:
        return self.robot_code or self.client_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DingTalkConfig":
        client_id = env_str("TABVIS_DINGTALK_CLIENT_ID", env=env)
        client_secret = env_str("TABVIS_DINGTALK_CLIENT_SECRET", env=env)
        if not client_id or not client_secret:
            raise RuntimeError(
                "TABVIS_DINGTALK_CLIENT_ID and TABVIS_DINGTALK_CLIENT_SECRET are required to "
                "configure the DingTalk channel"
            )
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            robot_code=env_str("TABVIS_DINGTALK_ROBOT_CODE", env=env),
        )


class DingTalkClient(RestChannelClient):
    def __init__(self, config: DingTalkConfig, *, client=None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        resp = await self.request_json(
            "POST",
            "/v1.0/oauth2/accessToken",
            json_body={"appKey": self._config.client_id, "appSecret": self._config.client_secret},
            auth=False,
        )
        token = resp.get("accessToken")
        if not token:
            raise ChannelApiError(
                f"dingtalk accessToken error: {resp.get('message')}", code=resp.get("code"), detail=resp
            )
        return token, float(resp.get("expireIn", 7200))

    async def send_text(self, conversation_id: str, text: str) -> str:
        """Send a plain-text message into a conversation; returns DingTalk's ``processQueryKey``.

        The v1.0 robot API takes the token as ``x-acs-dingtalk-access-token`` (not a bearer), and the
        text body is JSON-encoded into ``msgParam`` under the ``sampleText`` template key.
        """
        token = await self._ensure_token()
        resp = await self.request_json(
            "POST",
            "/v1.0/robot/groupMessages/send",
            json_body={
                "robotCode": self._config.effective_robot_code,
                "openConversationId": conversation_id,
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": text}, ensure_ascii=False),
            },
            auth=False,
            headers={_ACCESS_TOKEN_HEADER: token},
        )
        return self._process_key_or_raise(resp)

    @staticmethod
    def _process_key_or_raise(resp: dict) -> str:
        # v1.0 OpenAPI success carries ``processQueryKey``; an error carries a string ``code`` + ``message``.
        key = resp.get("processQueryKey")
        if not key:
            raise ChannelApiError(
                f"dingtalk send error: {resp.get('message')}", code=resp.get("code"), detail=resp
            )
        return key
