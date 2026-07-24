"""Envelope encryption for authenticated-session storage state (design §10.2).

Every session's Playwright ``storage_state`` (cookies + local/session storage) is encrypted with a
fresh per-session **DEK** (AES-256-GCM); the DEK is then wrapped by a **KEK** held in the OS
Keychain / a KMS / Vault (design §10.2). The database only ever holds the ciphertext, nonces,
algorithm version and key id — never plaintext, never the unwrapped DEK.

Two hard rules from §10.2:

* the data-encryption **AAD binds user + task + profile + session id**, so a ciphertext decrypted with
  the wrong context (a different task/user) fails authentication rather than yielding cookies;
* there is **no plaintext fallback** — if encryption is unavailable the caller must refuse to persist,
  not store cookies in the clear.

The KEK is supplied through a :class:`KeyProvider` so the local-Keychain vs external-KMS choice
(§18.5) is a wiring decision, not baked in here.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ALGORITHM_VERSION = 1
_NONCE_LEN = 12


class SessionCryptoError(RuntimeError):
    """Encryption or decryption failed. Callers MUST fail closed (never store/return plaintext)."""


class KeyProvider(Protocol):
    """Wraps/unwraps a DEK with a KEK held in a real key store (Keychain / KMS / Vault, §10.2)."""

    @property
    def key_id(self) -> str: ...

    def wrap(self, dek: bytes, *, aad: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes, *, aad: bytes, key_id: str) -> bytes: ...


class LocalKeyProvider:
    """KEK held as raw bytes (from the OS Keychain in production; injected in tests).

    A production wiring loads the 32-byte KEK from ``secret_store`` (a secure OS backend) under a stable
    ``key_id``; an external KMS/Vault provider implements the same interface remotely.
    """

    def __init__(self, kek: bytes, *, key_id: str = "local-kek-1") -> None:
        if len(kek) not in (16, 24, 32):
            raise ValueError("KEK must be 128/192/256-bit")
        self._kek = kek
        self._key_id = key_id

    @property
    def key_id(self) -> str:
        return self._key_id

    def wrap(self, dek: bytes, *, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        ct = AESGCM(self._kek).encrypt(nonce, dek, aad)
        return nonce + ct

    def unwrap(self, wrapped: bytes, *, aad: bytes, key_id: str) -> bytes:
        if key_id != self._key_id:
            raise SessionCryptoError("unknown key id")
        nonce, ct = wrapped[:_NONCE_LEN], wrapped[_NONCE_LEN:]
        try:
            return AESGCM(self._kek).decrypt(nonce, ct, aad)
        except InvalidTag as exc:
            raise SessionCryptoError("DEK unwrap failed") from exc


def build_aad(*, user_id: str, task_id: str, profile_id: str, session_id: str) -> bytes:
    """The data-encryption AAD binding user + task + profile + session (§10.2)."""
    return "|".join(["v1", user_id, task_id, profile_id, session_id]).encode("utf-8")


def encrypt_storage_state(
    storage_state: dict,
    *,
    key_provider: KeyProvider,
    user_id: str,
    task_id: str,
    profile_id: str,
    session_id: str,
) -> bytes:
    """Envelope-encrypt a storage-state dict. Returns the serialized envelope (bytes). Never plaintext."""
    try:
        plaintext = json.dumps(storage_state, separators=(",", ":")).encode("utf-8")
        aad = build_aad(
            user_id=user_id, task_id=task_id, profile_id=profile_id, session_id=session_id
        )
        dek = AESGCM.generate_key(bit_length=256)
        try:
            data_nonce = os.urandom(_NONCE_LEN)
            ciphertext = AESGCM(dek).encrypt(data_nonce, plaintext, aad)
            wrapped_dek = key_provider.wrap(dek, aad=f"kek:{key_provider.key_id}".encode())
        finally:
            dek = b"\x00" * len(dek)  # drop the DEK reference promptly
        envelope = {
            "algorithm_version": ALGORITHM_VERSION,
            "key_id": key_provider.key_id,
            "data_nonce": _b64(data_nonce),
            "ciphertext": _b64(ciphertext),
            "wrapped_dek": _b64(wrapped_dek),
        }
        return json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    except SessionCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001 - any crypto failure fails closed (§10.2)
        raise SessionCryptoError("encryption failed") from exc


def decrypt_storage_state(
    envelope_bytes: bytes,
    *,
    key_provider: KeyProvider,
    user_id: str,
    task_id: str,
    profile_id: str,
    session_id: str,
) -> dict:
    """Decrypt an envelope back to a storage-state dict, or raise :class:`SessionCryptoError`.

    Decryption authenticates the AAD, so passing a different (user, task, profile, session) than the one
    the ciphertext was created with fails rather than returning cookies (§10.2).
    """
    try:
        envelope = json.loads(envelope_bytes.decode("utf-8"))
        aad = build_aad(
            user_id=user_id, task_id=task_id, profile_id=profile_id, session_id=session_id
        )
        wrapped_dek = _unb64(envelope["wrapped_dek"])
        dek = key_provider.unwrap(
            wrapped_dek, aad=f"kek:{envelope['key_id']}".encode(), key_id=envelope["key_id"]
        )
        try:
            data_nonce = _unb64(envelope["data_nonce"])
            ciphertext = _unb64(envelope["ciphertext"])
            plaintext = AESGCM(dek).decrypt(data_nonce, ciphertext, aad)
        finally:
            dek = b"\x00" * len(dek)
        return json.loads(plaintext.decode("utf-8"))
    except SessionCryptoError:
        raise
    except (InvalidTag, KeyError, ValueError) as exc:
        raise SessionCryptoError("decryption failed") from exc


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text)
