"""Feishu / Lark webhook crypto: v2 event signature verification and encrypted-envelope decryption.

Feishu's inbound verification is its own scheme, not the framework's plain HMAC:

* **Signature** (v2 events, enforced when an Encrypt Key is set) —
  ``sha256_hex(timestamp + nonce + encrypt_key + raw_body)`` compared, constant-time, to the
  ``X-Lark-Signature`` header. Uses only the standard library.
* **Encrypted mode** — the body is ``{"encrypt": "<base64>"}``; decrypt with **AES-256-CBC**, key =
  ``sha256(encrypt_key)``, IV = the first 16 bytes of the ciphertext, PKCS#7 padding, to recover the
  event JSON string.

Decryption needs the ``cryptography`` package, imported lazily so a plaintext-mode Feishu bot (the
common case) works with no extra dependency. If an Encrypt Key is configured but ``cryptography`` is
missing, a clear install hint is raised — the same optional-extra pattern tabvis uses for the
openai/gemini/ocr providers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def feishu_signature(timestamp: str, nonce: str, encrypt_key: str, raw_body: bytes) -> str:
    """The hex SHA256 Feishu computes for the ``X-Lark-Signature`` header."""
    body_str = raw_body.decode("utf-8", errors="replace")
    content = f"{timestamp}{nonce}{encrypt_key}{body_str}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def verify_signature(
    encrypt_key: str, timestamp: str, nonce: str, raw_body: bytes, provided: str | None
) -> bool:
    """Constant-time verify of a Feishu v2 webhook signature. Any missing input fails closed."""
    if not encrypt_key or not timestamp or not nonce or not provided:
        return False
    expected = feishu_signature(timestamp, nonce, encrypt_key, raw_body)
    return hmac.compare_digest(expected, provided)


def decrypt_envelope(encrypt_key: str, encrypt_b64: str) -> str:
    """AES-256-CBC decrypt a Feishu ``{"encrypt": ...}`` payload back to its plaintext JSON string."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # optional extra, like the openai/gemini providers
        raise RuntimeError(
            "Feishu encrypted-event mode needs the 'cryptography' package. Install it with "
            "`uv sync --extra feishu`, or turn off Encrypt Key in the Feishu developer console."
        ) from exc

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()  # 32-byte AES-256 key
    data = base64.b64decode(encrypt_b64)
    if len(data) <= 16:
        raise ValueError("Feishu encrypted payload is too short")
    iv, ciphertext = data[:16], data[16:]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    if pad < 1 or pad > 16 or pad > len(plain):
        raise ValueError("invalid PKCS#7 padding in Feishu payload")
    return plain[:-pad].decode("utf-8")
