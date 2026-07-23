"""Common webhook-verification primitives shared by platform channels.

Each platform's exact signing scheme differs, but most reuse these two pieces: a constant-time
string compare, and HMAC-SHA256 hex over the raw body (the GitHub/Slack/Stripe style — this is also
what the framework's generic :mod:`tabvis.channels.core.signatures` uses). Schemes that are *not*
plain HMAC — Feishu's ``timestamp+nonce+key`` concat, Discord's Ed25519, DingTalk's timestamp sign —
live in their own plugin's crypto module and only borrow :func:`constant_time_eq` from here.
"""

from __future__ import annotations

import hashlib
import hmac


def constant_time_eq(a: str, b: str) -> bool:
    """Timing-safe string equality (both sides encoded to bytes)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hmac_sha256_hex(secret: str, body: bytes) -> str:
    """The hex HMAC-SHA256 a sender computes over the raw body."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_hmac_sha256(secret: str, body: bytes, provided: str | None) -> bool:
    """Constant-time verify of an HMAC-SHA256 signature; a missing secret or signature fails closed.

    Tolerates an optional ``sha256=`` prefix, as several providers send.
    """
    if not secret or not provided:
        return False
    expected = hmac_sha256_hex(secret, body)
    candidate = provided.split("=", 1)[1] if "=" in provided else provided
    return hmac.compare_digest(expected, candidate)
