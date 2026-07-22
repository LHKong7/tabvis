"""Matrix Client-Server REST client — ``whoami`` / ``/sync`` long-poll / room ``send``.

Matrix's live channel is plain HTTP long-poll (``GET /sync?timeout=30000``), not a websocket, and the
whole plaintext path is the Client-Server API with a ``Authorization: Bearer <access_token>`` header —
so this is a thin ``httpx`` wrapper. (Hermes wraps the ``mautrix`` SDK, but that is only required for
E2EE; plaintext rooms need no SDK.) All endpoints are under the homeserver base URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from tabvis.channels.plugins._platform.config import env_required, env_str
from tabvis.channels.plugins._platform.rest import ChannelApiError


@dataclass
class MatrixConfig:
    homeserver: str
    access_token: str
    user_id: str = ""          # @bot:server — resolved via whoami if unset; needed for self-message drop
    channel_account_id: str = ""
    sync_timeout_ms: int = 30000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "MatrixConfig":
        return cls(
            homeserver=env_required("TABVIS_MATRIX_HOMESERVER", env=env).rstrip("/"),
            access_token=env_required("TABVIS_MATRIX_ACCESS_TOKEN", env=env),
            user_id=env_str("TABVIS_MATRIX_USER_ID", env=env),
            channel_account_id=env_str("TABVIS_MATRIX_CHANNEL_ACCOUNT_ID", env=env),
        )


class MatrixClient:
    def __init__(self, config: MatrixConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60.0)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.access_token}"}

    def _url(self, path: str) -> str:
        return f"{self._config.homeserver}{path}"

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    async def whoami(self) -> str:
        response = await self._http().get(self._url("/_matrix/client/v3/account/whoami"), headers=self._headers())
        return self._json(response).get("user_id", "")

    async def sync(self, *, since: str | None = None, timeout_ms: int = 30000) -> dict:
        params: dict[str, Any] = {"timeout": timeout_ms}
        if since:
            params["since"] = since
        response = await self._http().get(
            self._url("/_matrix/client/v3/sync"),
            params=params,
            headers=self._headers(),
            timeout=(timeout_ms / 1000) + 15,  # outlast the server-side long-poll hold
        )
        return self._json(response)

    async def send_text(self, room_id: str, text: str, *, txn_id: str) -> str:
        # room_id ('!room:server') and the txn id must be URL-encoded into the path; a repeated txn id
        # de-dups server-side (we pass the delivery_id, so a retried delivery is idempotent end-to-end).
        url = self._url(
            f"/_matrix/client/v3/rooms/{quote(room_id, safe='')}/send/m.room.message/{quote(txn_id, safe='')}"
        )
        response = await self._http().put(url, json={"msgtype": "m.text", "body": text}, headers=self._headers())
        data = self._json(response)
        if response.status_code not in (200, 201) or not data.get("event_id"):
            raise ChannelApiError(f"matrix send error ({response.status_code})", code=response.status_code, detail=data)
        return data["event_id"]

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
