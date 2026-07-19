"""Runtime API authentication + management-face protection (P0-2).

``docs/permission-policy-engine_v1.md`` §"P0-2". The Runtime API server historically had no auth (a
localhost-only dev console). This wires an authenticated :class:`Principal` onto each request and
protects the management face:

* **Principal resolution** — an ``Authorization: Bearer <TABVIS_SERVER_ADMIN_TOKEN>`` is the management
  (admin) principal; otherwise the per-agent credential header (``x-tabvis-agent-credential``) resolves
  to that agent's :class:`Principal`. The ``agent_id`` comes from the authenticated credential, never
  from a request body — a business request cannot spoof it.
* **Loopback vs. remote** — on a loopback bind (the default) an unauthenticated request is treated as
  the local admin, preserving today's dev behavior (and the test client). Auth is *required* only when
  the server binds a non-loopback address.
* **Startup guard** — binding a non-loopback address without an admin token configured is refused
  (:func:`enforce_startup_auth`), so a remote-exposed server is never wide open.

Transport hardening (security headers, a body-size cap, and a default-deny CORS posture) is a pure
ASGI middleware (:class:`SecurityMiddleware`) that only touches the response *start* — safe for the
SSE/streaming routes.
"""

from __future__ import annotations

import os
from typing import Any

from tabvis.policy.runtime_adapter import Principal

_ADMIN_TOKEN_ENV = "TABVIS_SERVER_ADMIN_TOKEN"
_CORS_ENV = "TABVIS_SERVER_CORS_ORIGINS"
_MAX_BODY_ENV = "TABVIS_SERVER_MAX_BODY_BYTES"
_DEFAULT_MAX_BODY = 10 * 1024 * 1024  # 10 MiB

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0.local"})


def is_loopback(host: str | None) -> bool:
    """Whether ``host`` is a loopback bind. ``0.0.0.0`` / ``::`` are NOT loopback (they are public)."""
    if not host:
        return True
    return host.strip().lower() in _LOOPBACK_HOSTS


def auth_required_for_host(host: str | None) -> bool:
    return not is_loopback(host)


def admin_token() -> str | None:
    tok = os.environ.get(_ADMIN_TOKEN_ENV)
    return tok.strip() if tok and tok.strip() else None


def enforce_startup_auth(host: str | None) -> None:
    """Refuse to start a non-loopback server that has no admin token configured (P0-2)."""
    if auth_required_for_host(host) and admin_token() is None:
        raise SystemExit(
            f"Refusing to bind {host!r} without authentication: set {_ADMIN_TOKEN_ENV} to a secret "
            f"token (the server would otherwise expose agent management to the network)."
        )


def _bearer(headers: Any) -> str | None:
    try:
        raw = headers.get("authorization")
    except Exception:  # noqa: BLE001
        raw = None
    if not raw or not raw.lower().startswith("bearer "):
        return None
    return raw[7:].strip() or None


def resolve_principal(headers: Any, *, auth_required: bool) -> Principal | None:
    """Resolve the request Principal, or None when auth is required but the request is unauthenticated.

    Admin bearer token → admin principal; agent credential header → that agent; otherwise, on a
    loopback (non-required) bind, the local admin; on a required bind, None (caller must 401).
    """
    from tabvis.agent.agents import credentials

    tok = admin_token()
    if tok is not None and _bearer(headers) == tok:
        return Principal(is_admin=True)

    agent_id = credentials.agent_id_from_request_headers(headers)
    if agent_id:
        return Principal(agent_id=agent_id)

    if not auth_required:
        return Principal(is_admin=True)  # local dev / loopback — open, as before
    return None


def max_body_bytes() -> int:
    raw = os.environ.get(_MAX_BODY_ENV)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _DEFAULT_MAX_BODY


def _cors_origin(request_origin: str | None) -> str | None:
    """The Access-Control-Allow-Origin value for ``request_origin`` under the configured allowlist."""
    allowed = os.environ.get(_CORS_ENV)
    if not allowed:
        return None  # default deny cross-origin
    origins = {o.strip() for o in allowed.split(",") if o.strip()}
    if "*" in origins:
        return "*"
    if request_origin and request_origin in origins:
        return request_origin
    return None


_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


class SecurityMiddleware:
    """Pure-ASGI transport hardening: security headers, body-size cap, default-deny CORS.

    Only rewrites the response *start* message, so streaming/SSE responses are untouched. A request
    whose ``Content-Length`` exceeds the cap is rejected with 413 before the handler runs.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        cl = headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_body_bytes():
                    await _send_status(send, 413, b"request body too large")
                    return
            except ValueError:
                pass

        cors = _cors_origin(headers.get("origin"))

        async def _send(message: Any) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers", []))
                raw.extend(_SECURITY_HEADERS)
                if cors is not None:
                    raw.append((b"access-control-allow-origin", cors.encode()))
                    raw.append((b"vary", b"Origin"))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, _send)


async def _send_status(send: Any, code: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": code,
                "headers": [(b"content-type", b"text/plain"), *_SECURITY_HEADERS]})
    await send({"type": "http.response.body", "body": body})
