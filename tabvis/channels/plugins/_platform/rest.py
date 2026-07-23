"""``RestChannelClient`` — a small async HTTP client base for token-authenticated platform APIs.

Most messaging platforms authenticate outbound sends with a short-lived bearer token obtained from an
app id/secret (Feishu's ``tenant_access_token``, WeCom's ``access_token``, DingTalk's, …). This base
caches the token and refreshes it a margin before expiry, so each platform client only implements
:meth:`_fetch_token` (how to get one) and its send/reply methods. Built on ``httpx`` (a tabvis
dependency); a caller may inject an ``httpx.AsyncClient`` (e.g. an ``httpx.MockTransport``) for tests.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class ChannelApiError(RuntimeError):
    """A platform API returned a non-success response."""

    def __init__(self, message: str, *, code: Any = None, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class RestChannelClient:
    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        token_refresh_margin: float = 300.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._margin = token_refresh_margin
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _fetch_token(self) -> tuple[str, float]:
        """Return ``(token, ttl_seconds)``. Subclasses implement the platform's token endpoint."""
        raise NotImplementedError

    async def _ensure_token(self) -> str:
        now = time.monotonic()
        if self._token is None or now >= self._token_expiry:
            token, ttl = await self._fetch_token()
            self._token = token
            self._token_expiry = now + max(0.0, float(ttl) - self._margin)
        return self._token

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        auth: bool = True,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        if auth:
            request_headers["Authorization"] = f"Bearer {await self._ensure_token()}"
        url = path if path.startswith("http") else f"{self._base}{path}"
        response = await self._http().request(
            method, url, json=json_body, params=params, headers=request_headers
        )
        try:
            data = response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body becomes an empty envelope, not a crash
            data = {}
        return data if isinstance(data, dict) else {"data": data}

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
