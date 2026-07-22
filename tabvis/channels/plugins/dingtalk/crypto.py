"""钉钉 DingTalk outgoing-robot callback verification.

DingTalk's live bot ships as **Stream Mode**: the ``dingtalk-stream`` SDK opens an outbound
WebSocket and DingTalk pushes events down it. ``httpx`` doesn't do WebSockets, so that path can't be
reached with ``stdlib + httpx + cryptography`` alone. This plugin therefore speaks DingTalk's *other*
supported inbound shape — the classic **HTTP outgoing-robot callback**, where DingTalk POSTs each
message to our HTTPS endpoint and signs it with a ``timestamp`` + ``sign`` header pair.

That scheme is its own thing, not the framework's plain HMAC-over-body:

* **Signature** — ``base64(HMAC_SHA256(key=app_secret, msg="{timestamp}\\n{app_secret}"))`` compared,
  constant-time, to the ``sign`` header. Note it signs *only* ``timestamp+secret``, **not the body**:
  it authenticates the caller (proves it knows the app secret) and, with the freshness window below,
  thwarts replay — but does not bind the payload. That is DingTalk's documented scheme, quirks and all.
* **Freshness** — ``timestamp`` is epoch **milliseconds**; DingTalk enforces a ~1h window, so a stale
  callback is rejected as a replay.

Everything is computable with the standard library — no AES envelope exists (DingTalk's callback is
plain JSON), so unlike Feishu this module never needs the ``cryptography`` package.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# DingTalk sends the callback timestamp in epoch-ms and enforces a ~1h validity window server-side.
DEFAULT_TOLERANCE_MS = 3_600_000


def dingtalk_sign(timestamp: str, app_secret: str) -> str:
    """The base64 HMAC-SHA256 DingTalk puts in the ``sign`` header of an outgoing-robot callback."""
    string_to_sign = f"{timestamp}\n{app_secret}".encode("utf-8")
    digest = hmac.new(app_secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(
    app_secret: str,
    timestamp: str,
    provided: str | None,
    *,
    tolerance_ms: int | None = DEFAULT_TOLERANCE_MS,
) -> bool:
    """Constant-time verify of a DingTalk outgoing-robot ``sign``. Any missing input fails closed.

    ``tolerance_ms`` bounds the accepted clock skew (replay guard); pass ``None`` to skip the window
    check (used by the crypto unit test with a fixed timestamp).
    """
    if not app_secret or not timestamp or not provided:
        return False
    if tolerance_ms is not None:
        try:
            skew = abs(time.time() * 1000 - float(timestamp))
        except (TypeError, ValueError):
            return False  # non-numeric timestamp — a malformed/forged callback
        if skew > tolerance_ms:
            return False
    expected = dingtalk_sign(timestamp, app_secret)
    return hmac.compare_digest(expected, provided)
