"""QQ official bot webhook crypto — Ed25519 signature verify + the validation handshake.

QQ's webhook auth is Ed25519, keyed off the bot **AppSecret**: the secret string is repeated until it
reaches 32 bytes and used as the Ed25519 private-key seed (per Tencent's ``botpy`` SDK). Two operations:

* **Verify an event** — the request carries ``X-Signature-Ed25519`` (hex) and ``X-Signature-Timestamp``;
  the signed message is ``timestamp_bytes + raw_body``.
* **Validation handshake** (``op == 13``) — the callback URL is verified by signing ``event_ts +
  plain_token`` and returning ``{"plain_token", "signature"}``.

Ed25519 needs the ``cryptography`` package, imported lazily with a clear install hint (the same
optional-extra pattern as feishu/wecom/teams).
"""

from __future__ import annotations


def _seed(secret: str) -> bytes:
    """Repeat the AppSecret to exactly 32 bytes — the Ed25519 private-key seed (Tencent's scheme)."""
    if not secret:
        raise ValueError("QQ bot secret is required for Ed25519 signing")
    seed = secret
    while len(seed) < 32:
        seed += secret
    return seed[:32].encode("utf-8")


def _private_key(secret: str):
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # optional extra, like the feishu/wecom crypto paths
        raise RuntimeError(
            "The QQ bot channel needs the 'cryptography' package for Ed25519. Install it with "
            "`uv sync --extra qq`."
        ) from exc
    return Ed25519PrivateKey.from_private_bytes(_seed(secret))


def sign_validation(secret: str, event_ts: str, plain_token: str) -> str:
    """Sign ``event_ts + plain_token`` for the op-13 callback validation; returns hex signature."""
    message = (str(event_ts) + str(plain_token)).encode("utf-8")
    return _private_key(secret).sign(message).hex()


def verify_event(secret: str, timestamp: str, raw_body: bytes, signature_hex: str | None) -> bool:
    """Verify an event's Ed25519 signature over ``timestamp + raw_body``. Missing input fails closed."""
    if not secret or not timestamp or not signature_hex:
        return False
    from cryptography.exceptions import InvalidSignature

    public_key = _private_key(secret).public_key()
    message = str(timestamp).encode("utf-8") + raw_body
    try:
        public_key.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError):
        return False
