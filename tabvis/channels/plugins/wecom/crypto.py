"""企业微信 / WeCom callback crypto: SHA1 URL signature + WXBizMsgCrypt AES envelope decryption.

WeCom's inbound verification is its own scheme (Tencent's ``WXBizMsgCrypt`` wire format), not the
framework's plain HMAC:

* **Signature** — ``sha1_hex(sorted([token, timestamp, nonce, encrypt]) joined)``. The four strings
  are sorted *lexicographically*, concatenated with no separator, hashed with SHA1, hex-digested, and
  compared constant-time to the ``msg_signature`` query param. It is a plain SHA1 (the Token is just
  one of the sorted parts, not an HMAC key), so it needs only the standard library.
* **Encrypted mode** — the ciphertext (the GET ``echostr`` or the POST body's ``<Encrypt>``) is an
  AES-256-**CBC** blob. The real AES key is ``base64decode(EncodingAESKey + "=")`` → 32 bytes; the IV
  is the **first 16 bytes of that key** (deterministic, *not* random per message). After decrypt the
  layout is ``random16 | uint32 msg_len (network byte order) | xml | receive_id`` under **PKCS#7 with
  a 32-byte block** (Tencent's quirk — not 16). ``receive_id`` must equal the corp id.

Decryption needs the ``cryptography`` package, imported lazily so the module imports cleanly even in
an environment without it — the same optional-extra pattern the Feishu plugin uses.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import socket
import struct

_AES_BLOCK_SIZE = 32  # WXBizMsgCrypt pads with PKCS#7 to a 32-byte block, not the AES 16-byte block.


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """The hex SHA1 WeCom computes for ``msg_signature`` — sorted concat of the four strings."""
    parts = sorted([token, timestamp, nonce, encrypt])  # lexicographic sort, then join with no sep
    return hashlib.sha1("".join(parts).encode("utf-8")).hexdigest()


def verify_signature(
    token: str, timestamp: str, nonce: str, encrypt: str, provided: str | None
) -> bool:
    """Constant-time verify of a WeCom callback signature. Any missing input fails closed."""
    if not token or not timestamp or not nonce or not encrypt or not provided:
        return False
    expected = wecom_signature(token, timestamp, nonce, encrypt)
    return hmac.compare_digest(expected, provided)


def aes_key(encoding_aes_key: str) -> bytes:
    """Decode a 43-char EncodingAESKey to the 32-byte AES-256 key (``b64decode(key + "=")``)."""
    if len(encoding_aes_key) != 43:
        raise ValueError("WeCom EncodingAESKey must be exactly 43 characters")
    return base64.b64decode(encoding_aes_key + "=")


def decrypt_message(encoding_aes_key: str, receive_id: str, encrypt_b64: str) -> str:
    """AES-256-CBC decrypt a WeCom ciphertext back to its plaintext XML (or the ``echostr`` payload).

    Applies to both the GET ``echostr`` handshake and a POST body's ``<Encrypt>``. Raises on a bad
    key length, short/garbage ciphertext, or a ``receive_id`` that isn't our corp id.
    """
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:  # optional extra, like the feishu/openai/gemini providers
        raise RuntimeError(
            "WeCom callback mode needs the 'cryptography' package. Install it with "
            "`uv sync --extra wecom` (AES-256-CBC decryption of the callback envelope)."
        ) from exc

    key = aes_key(encoding_aes_key)          # 32-byte AES-256 key
    iv = key[:16]                            # WeCom fixes the IV to the first 16 key bytes
    cipher_text = base64.b64decode(encrypt_b64)
    if not cipher_text or len(cipher_text) % 16 != 0:
        raise ValueError("WeCom ciphertext is not a whole number of AES blocks")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = decryptor.update(cipher_text) + decryptor.finalize()

    # Strip PKCS#7 padding — WeCom's block is 32, so the pad byte ranges 1..32.
    pad = plain[-1] if plain else 0
    if pad < 1 or pad > _AES_BLOCK_SIZE or pad > len(plain):
        raise ValueError("invalid PKCS#7 padding in WeCom payload")
    content = plain[:-pad]

    # Layout: [0:16] random prefix | [16:20] uint32 length | [20:20+len] xml | [20+len:] receive_id.
    if len(content) < 20:
        raise ValueError("WeCom decrypted payload is too short")
    # The length is network byte order (big-endian); ntohl(unpack-native) reads it portably.
    msg_len = socket.ntohl(struct.unpack("I", content[16:20])[0])
    if msg_len < 0 or 20 + msg_len > len(content):
        raise ValueError("WeCom declared message length is out of range")
    xml_content = content[20 : 20 + msg_len]
    from_receive_id = content[20 + msg_len :].decode("utf-8")
    if receive_id and from_receive_id != receive_id:
        raise ValueError("WeCom receive_id mismatch")
    return xml_content.decode("utf-8")
