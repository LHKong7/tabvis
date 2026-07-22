"""企业微信 / WeCom REST client — cached ``access_token``, plus app-message send.

Wraps the two WeCom API endpoints an outbound text needs:

* ``GET /cgi-bin/gettoken?corpid=..&corpsecret=..`` — exchange corp id/secret for an
  ``access_token`` (cached + refreshed by :class:`RestChannelClient`).
* ``POST /cgi-bin/message/send?access_token=..`` — send an application text message.

Unlike Feishu (Bearer header), WeCom carries the token as the ``access_token`` **query parameter**, so
this client fetches the cached token itself and passes it as a param rather than letting the base add
an ``Authorization`` header. WeCom can also reject a token mid-flight (``errcode`` 40001/42001) even
when it looks fresh, so :meth:`send_text` evicts the cache and refetches once on those codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient

_API_BASE = "https://qyapi.weixin.qq.com"
_DEFAULT_TOKEN_TTL = 7200.0          # WeCom access_token lives 7200s by default.
_MAX_TEXT_LEN = 2048                 # message/send text content cap.
_TOKEN_REJECTED = {40001, 42001}     # invalid / expired access_token — evict and retry once.


@dataclass
class WeComConfig:
    """One configured WeCom self-built app. Credentials, never read from event payloads."""

    corp_id: str                     # corpid for token fetch; also the crypto receive_id
    corp_secret: str                 # corpsecret for token fetch
    agent_id: str = ""               # numeric agentid for outbound message/send
    token: str = ""                  # callback Token — one of the SHA1 signature parts
    encoding_aes_key: str = ""       # 43-char EncodingAESKey — the AES-256-CBC key material

    @property
    def base_url(self) -> str:
        return _API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WeComConfig":
        corp_id = env_str("TABVIS_WECOM_CORP_ID", env=env)
        corp_secret = env_str("TABVIS_WECOM_CORP_SECRET", env=env)
        agent_id = env_str("TABVIS_WECOM_AGENT_ID", env=env)
        token = env_str("TABVIS_WECOM_TOKEN", env=env)
        encoding_aes_key = env_str("TABVIS_WECOM_ENCODING_AES_KEY", env=env)
        missing = [
            name
            for name, value in (
                ("TABVIS_WECOM_CORP_ID", corp_id),
                ("TABVIS_WECOM_CORP_SECRET", corp_secret),
                ("TABVIS_WECOM_AGENT_ID", agent_id),
                ("TABVIS_WECOM_TOKEN", token),
                ("TABVIS_WECOM_ENCODING_AES_KEY", encoding_aes_key),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required WeCom config: " + ", ".join(missing)
            )
        return cls(
            corp_id=corp_id,
            corp_secret=corp_secret,
            agent_id=agent_id,
            token=token,
            encoding_aes_key=encoding_aes_key,
        )


class WeComClient(RestChannelClient):
    def __init__(self, config: WeComConfig, *, client=None) -> None:
        # WeCom refreshes its token with a small (~60s) safety margin, not Feishu's 5-minute one.
        super().__init__(config.base_url, client=client, token_refresh_margin=60.0)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        resp = await self.request_json(
            "GET",
            "/cgi-bin/gettoken",
            params={"corpid": self._config.corp_id, "corpsecret": self._config.corp_secret},
            auth=False,
        )
        if resp.get("errcode") != 0 or not resp.get("access_token"):
            raise ChannelApiError(
                f"wecom gettoken error: {resp.get('errmsg')}", code=resp.get("errcode"), detail=resp
            )
        return resp["access_token"], float(resp.get("expires_in", _DEFAULT_TOKEN_TTL))

    async def send_text(self, user_id: str, text: str) -> str:
        """Send an application text message to a WeCom userid; returns the WeCom ``msgid``."""
        body = {
            "touser": user_id,
            "msgtype": "text",
            "agentid": self._agent_id(),
            "text": {"content": text[:_MAX_TEXT_LEN]},
            "safe": 0,
        }
        last: dict = {}
        for attempt in range(2):  # at most two tries: a fresh send, then one token-refresh retry
            token = await self._ensure_token()
            resp = await self.request_json(
                "POST",
                "/cgi-bin/message/send",
                params={"access_token": token},
                json_body=body,
                auth=False,  # token rides in the query param, not an Authorization header
            )
            errcode = resp.get("errcode")
            if errcode == 0:
                return str(resp.get("msgid") or "")
            last = resp
            # A rejected token can happen even when ours looks unexpired — drop it and refetch once.
            if errcode in _TOKEN_REJECTED and attempt == 0:
                self._invalidate_token()
                continue
            break
        raise ChannelApiError(
            f"wecom message/send error: {last.get('errmsg')}", code=last.get("errcode"), detail=last
        )

    def _agent_id(self) -> int:
        try:
            return int(self._config.agent_id)
        except (TypeError, ValueError):
            return 0

    def _invalidate_token(self) -> None:
        """Force the next call to refetch (RestChannelClient caches token + expiry on ``self``)."""
        self._token = None
        self._token_expiry = 0.0
