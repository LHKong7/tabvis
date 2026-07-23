"""WhatsApp Cloud (Meta Graph) webhook crypto: X-Hub-Signature-256 verification.

WhatsApp's inbound verification is its own scheme, not the framework's plain HMAC — the difference is
the mandatory ``sha256=`` prefix and the "sign the *raw* body bytes" rule, so this plugin declares
``signed_webhooks=False`` and verifies here rather than through the generic gateway gate:

* **Signature** — ``hmac_sha256_hex(app_secret, raw_body)`` compared, constant-time, to the value in
  the ``X-Hub-Signature-256`` header, which arrives as ``sha256=<lowercase hex>``. The MAC covers the
  exact bytes Meta sent; re-serializing the parsed JSON would change whitespace/key order and break
  the check, so the caller must pass the untouched request body.

Unlike Feishu there is **no encrypted envelope and no AES** — WhatsApp Cloud is plain JSON over HTTPS —
so this module needs only the standard library (``hmac`` + ``hashlib``); ``cryptography`` is not used.
"""

from __future__ import annotations

import hashlib
import hmac

_SIG_PREFIX = "sha256="


def whatsapp_signature(app_secret: str, raw_body: bytes) -> str:
    """The value Meta puts in ``X-Hub-Signature-256``: ``sha256=`` + hex HMAC-SHA256 of the raw body."""
    digest = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"{_SIG_PREFIX}{digest}"


def verify_signature(app_secret: str, raw_body: bytes, provided: str | None) -> bool:
    """Constant-time verify of an ``X-Hub-Signature-256`` header; anything missing/malformed fails closed.

    The header must carry the literal ``sha256=`` prefix — a bare hex string is rejected, matching
    Meta's exact contract (the algorithm is pinned by that prefix, not inferred).
    """
    if not app_secret or not provided or not provided.startswith(_SIG_PREFIX):
        return False
    expected = whatsapp_signature(app_secret, raw_body)
    return hmac.compare_digest(expected.lower().encode("utf-8"), provided.lower().encode("utf-8"))
