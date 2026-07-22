"""Google Chat REST client — Service Account access token, plus message create.

Wraps the two Google endpoints an outbound text needs:

* ``POST https://oauth2.googleapis.com/token`` — exchange a self-signed SA JWT-bearer assertion for a
  short-lived OAuth2 access token (scope ``chat.bot``), cached + refreshed by :class:`RestChannelClient`.
* ``POST https://chat.googleapis.com/v1/{space}/messages`` — create a message in a space.

There is no Google SDK here: the assertion is minted with ``cryptography`` (see :mod:`crypto`) and both
calls go out over ``httpx``. The token endpoint is the one place the base client's JSON helper does not
fit — OAuth2 wants a form-encoded body — so :meth:`_fetch_token` posts the form itself; every other
call uses the inherited bearer-attaching :meth:`request_json`.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Mapping

from tabvis.channels.plugins._platform.config import env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError, RestChannelClient
from tabvis.channels.plugins.google_chat.crypto import (
    CHAT_BOT_SCOPE,
    GOOGLE_TOKEN_URI,
    JWT_BEARER_GRANT,
    sign_service_account_assertion,
)

# The Chat REST host. A message create is ``POST {API_BASE}/v1/{space}/messages`` where the space
# resource name (``spaces/AAAA``) is the parent — this resolves to the same URL googleapiclient uses.
_API_BASE = "https://chat.googleapis.com"

# Chat's hard text limit is 4096 chars; Hermes chunks at 4000 for margin. A minimal text plugin keeps
# the constant so a caller can pre-split, but does not itself chunk.
MAX_MESSAGE_LENGTH = 4000

# Reply-in-thread only takes effect with this option; without it Google ignores ``thread.name`` and
# opens a fresh thread (adapter.py:2580-2598).
REPLY_FALLBACK_TO_NEW_THREAD = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"


def _load_service_account(raw: str) -> dict:
    """Parse the SA credential: inline JSON (starts with ``{``) or a path to the SA key file."""
    text = raw.strip()
    if text.startswith("{"):
        return _json.loads(text)
    with open(text, "r", encoding="utf-8") as handle:
        return _json.load(handle)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass
class GoogleChatConfig:
    """One configured Google Chat bot. Credentials + the inbound-verification expectations.

    The SA private key drives *outbound* sends; ``audience`` and ``caller_service_account_emails`` are
    the two values the *inbound* OIDC bearer is checked against (Google's ``aud`` and ``email`` claims).
    """

    service_account_email: str          # SA ``client_email`` — the ``iss`` of our outbound assertion
    private_key: str                    # SA PEM private key — signs the outbound assertion
    private_key_id: str = ""            # SA ``private_key_id`` — the assertion's ``kid``
    token_uri: str = GOOGLE_TOKEN_URI   # SA ``token_uri`` — where the assertion is exchanged
    webhook_url: str = ""               # the public HTTPS callback Google POSTs events to
    audience: str = ""                  # expected inbound ``aud`` (defaults to webhook_url)
    caller_service_account_emails: tuple[str, ...] = ()  # expected inbound ``email`` (Google caller SA)
    allowed_users: tuple[str, ...] = field(default_factory=tuple)  # optional sender-email allowlist
    home_channel: str = ""              # default space for unsolicited notifications, e.g. spaces/AAAA

    @property
    def base_url(self) -> str:
        return _API_BASE

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "GoogleChatConfig":
        sa_raw = env_str("TABVIS_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON", env=env)
        if not sa_raw:
            raise RuntimeError(
                "TABVIS_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON (path to, or inline, the SA key JSON) is "
                "required to configure the Google Chat channel"
            )
        info = _load_service_account(sa_raw)
        client_email = str(info.get("client_email") or "")
        private_key = str(info.get("private_key") or "")
        if not client_email or not private_key:
            raise RuntimeError(
                "TABVIS_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON must contain 'client_email' and 'private_key'"
            )
        webhook_url = env_str("TABVIS_GOOGLE_CHAT_WEBHOOK_URL", env=env)
        return cls(
            service_account_email=client_email,
            private_key=private_key,
            private_key_id=str(info.get("private_key_id") or ""),
            token_uri=str(info.get("token_uri") or GOOGLE_TOKEN_URI),
            webhook_url=webhook_url,
            # Google defaults the expected audience to the callback URL itself (adapter.py:753-757).
            audience=env_str("TABVIS_GOOGLE_CHAT_AUDIENCE", webhook_url, env=env),
            caller_service_account_emails=_split_csv(env_str("TABVIS_GOOGLE_CHAT_SA_EMAIL", env=env)),
            allowed_users=_split_csv(env_str("TABVIS_GOOGLE_CHAT_ALLOWED_USERS", env=env)),
            home_channel=env_str("TABVIS_GOOGLE_CHAT_HOME_CHANNEL", env=env),
        )


class GoogleChatClient(RestChannelClient):
    def __init__(self, config: GoogleChatConfig, *, client=None) -> None:
        super().__init__(config.base_url, client=client)
        self._config = config

    async def _fetch_token(self) -> tuple[str, float]:
        """Mint an SA access token: sign a JWT-bearer assertion, POST it form-encoded to the token URI.

        This is the one call that does not carry a bearer (the assertion *is* the credential) and is not
        JSON-bodied, so it goes out directly instead of through :meth:`request_json`.
        """
        assertion = sign_service_account_assertion(
            client_email=self._config.service_account_email,
            private_key_pem=self._config.private_key,
            private_key_id=self._config.private_key_id,
            scope=CHAT_BOT_SCOPE,
            token_uri=self._config.token_uri,
        )
        response = await self._http().request(
            "POST",
            self._config.token_uri,
            data={"grant_type": JWT_BEARER_GRANT, "assertion": assertion},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON token response is an error, not a crash
            payload = {}
        if not isinstance(payload, dict) or not payload.get("access_token"):
            detail = payload if isinstance(payload, dict) else {}
            raise ChannelApiError(
                f"google chat token error: {detail.get('error_description') or detail.get('error') or 'no access_token'}",
                code=detail.get("error"),
                detail=payload,
            )
        return str(payload["access_token"]), float(payload.get("expires_in", 3600))

    async def send_text(self, space_name: str, text: str, *, thread_name: str | None = None) -> str:
        """Create a plain-text message in ``space_name`` (``spaces/AAAA``); returns the message name.

        To land inside an existing thread we must set both ``thread.name`` and the reply-option query
        param — Google silently ignores the former without the latter.
        """
        body: dict = {"text": text}
        params: dict | None = None
        if thread_name:
            body["thread"] = {"name": thread_name}
            params = {"messageReplyOption": REPLY_FALLBACK_TO_NEW_THREAD}
        resp = await self.request_json(
            "POST", f"/v1/{space_name}/messages", json_body=body, params=params
        )
        return self._message_name_or_raise(resp)

    @staticmethod
    def _message_name_or_raise(resp: dict) -> str:
        # Success is purely the HTTP status (<400) — there is no ``ok`` wrapper. A failure body carries a
        # Google ``error`` object; on success we read the created message's resource ``name`` as its id.
        error = resp.get("error")
        if error:
            detail = error if isinstance(error, dict) else {"message": str(error)}
            raise ChannelApiError(
                f"google chat send error: {detail.get('message')}", code=detail.get("code"), detail=resp
            )
        return str(resp.get("name") or "")
