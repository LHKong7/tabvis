"""LINE webhook crypto: ``X-Line-Signature`` verification.

LINE's inbound verification is its own scheme, not the framework's plain-HMAC-*hex* gate — the digest
is **base64**-encoded, not hex — so it lives here and the plugin declares ``signed_webhooks=False`` and
verifies itself:

* **Signature** — ``base64(HMAC_SHA256(channel_secret, raw_body))`` compared, constant-time, to the
  ``X-Line-Signature`` header. The base string is the *exact raw request bytes*, so the plugin must
  verify before it parses (and never re-serializes) the JSON. Standard library only — LINE is JSON +
  HMAC end to end, with no AES envelope, no XML, and no ``url_verification`` challenge.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def line_signature(channel_secret: str, raw_body: bytes) -> str:
    """The base64 SHA256-HMAC LINE computes for the ``X-Line-Signature`` header."""
    digest = hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_line_signature(channel_secret: str, raw_body: bytes, provided: str | None) -> bool:
    """Constant-time verify of a LINE webhook signature. Any missing input fails closed.

    Compared as bytes: ``compare_digest`` raises on a ``str`` carrying non-ASCII, and the header is an
    attacker-controlled value.
    """
    if not channel_secret or not provided or raw_body is None:
        return False
    expected = line_signature(channel_secret, raw_body)
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
