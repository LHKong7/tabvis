"""Webhook signature verification (design §4.5, §4.7).

"Verify webhook signature before parsing content" is the first step of the inbound flow (design §4.5):
the raw body is authenticated before any field is trusted. This is the standard HMAC-SHA256 scheme
(GitHub/Slack/Stripe style), with a constant-time compare so verification does not leak via timing.
"""

from __future__ import annotations

import hashlib
import hmac


def sign(secret: str, body: bytes) -> str:
    """The hex HMAC-SHA256 a sender computes over the raw body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, provided_signature: str | None) -> bool:
    """Constant-time verify. A missing signature or secret fails closed."""
    if not secret or not provided_signature:
        return False
    expected = sign(secret, body)
    # Accept an optional "sha256=" prefix, as several providers send.
    candidate = provided_signature.split("=", 1)[1] if "=" in provided_signature else provided_signature
    return hmac.compare_digest(expected, candidate)
