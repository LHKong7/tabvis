"""Slack webhook crypto: Events API request-signature verification.

Slack signs every Events API request with a scheme that is *almost* plain HMAC but not quite — it
hashes a versioned base string, prefixes the digest, and layers a replay window on top — so it lives
here rather than borrowing the framework's :func:`verify_hmac_sha256` (which hashes the raw body
alone). Everything here is standard library; Slack is hex-HMAC + JSON with no envelope encryption
(contrast Feishu's AES mode), so there is nothing to lazily import.

The scheme (Slack "Verifying requests" docs):

* **Base string** — ``v0:{timestamp}:{raw_body}``: the literal version tag ``v0``, a colon, the
  ``X-Slack-Request-Timestamp`` header value, a colon, then the **raw** (unparsed) request bytes.
* **Digest** — HMAC-SHA256 keyed by the signing secret, lowercase hex, prefixed with ``v0=``, then
  compared constant-time against the ``X-Slack-Signature`` header.
* **Replay guard** — reject when the timestamp is more than ``tolerance`` seconds (Slack's default is
  300) from now, so a captured request cannot be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac
import time

SIGNATURE_VERSION = "v0"
DEFAULT_TOLERANCE_SECONDS = 300


def slack_base_string(timestamp: str, raw_body: bytes) -> bytes:
    """The ``v0:{timestamp}:{raw_body}`` string Slack signs, as bytes (raw body stays untouched)."""
    return b"v0:" + timestamp.encode("utf-8") + b":" + raw_body


def slack_signature(signing_secret: str, timestamp: str, raw_body: bytes) -> str:
    """The ``v0=<hex>`` value Slack puts in the ``X-Slack-Signature`` header."""
    digest = hmac.new(
        signing_secret.encode("utf-8"), slack_base_string(timestamp, raw_body), hashlib.sha256
    ).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def verify_signature(
    signing_secret: str,
    timestamp: str,
    raw_body: bytes,
    provided: str | None,
    *,
    tolerance: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Constant-time verify of a Slack request signature; any missing input or stale timestamp fails
    closed. ``now`` is injectable so a test can exercise the replay guard without touching the clock."""
    if not signing_secret or not timestamp or not provided:
        return False
    try:
        event_ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = time.time() if now is None else now
    if abs(current - event_ts) > tolerance:  # replay window — an old capture can't be re-sent
        return False
    expected = slack_signature(signing_secret, timestamp, raw_body)
    return hmac.compare_digest(expected, provided)
