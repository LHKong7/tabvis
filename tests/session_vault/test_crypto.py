"""Envelope encryption (design §10.2, §16.1)."""

from __future__ import annotations

import os

import pytest

from tabvis.session_vault.crypto import (
    SessionCryptoError,
    LocalKeyProvider,
    decrypt_storage_state,
    encrypt_storage_state,
)

_STATE = {"cookies": [{"name": "sid", "value": "abc123"}], "origins": []}


def _kp() -> LocalKeyProvider:
    return LocalKeyProvider(os.urandom(32))


def _ctx():
    return dict(user_id="u1", task_id="t1", profile_id="p1", session_id="s1")


def test_round_trip() -> None:
    kp = _kp()
    env = encrypt_storage_state(_STATE, key_provider=kp, **_ctx())
    assert isinstance(env, bytes)
    out = decrypt_storage_state(env, key_provider=kp, **_ctx())
    assert out == _STATE


def test_ciphertext_holds_no_plaintext() -> None:
    env = encrypt_storage_state(_STATE, key_provider=_kp(), **_ctx())
    assert b"abc123" not in env  # the cookie value never appears in the envelope
    assert b"sid" not in env


def test_wrong_aad_context_fails() -> None:
    kp = _kp()
    env = encrypt_storage_state(_STATE, key_provider=kp, **_ctx())
    # decrypting with a different task must fail (AAD binds user+task+profile+session, §10.2)
    with pytest.raises(SessionCryptoError):
        decrypt_storage_state(
            env, key_provider=kp, user_id="u1", task_id="OTHER", profile_id="p1", session_id="s1"
        )


def test_wrong_kek_fails() -> None:
    env = encrypt_storage_state(_STATE, key_provider=_kp(), **_ctx())
    other = LocalKeyProvider(os.urandom(32))  # different KEK
    with pytest.raises(SessionCryptoError):
        decrypt_storage_state(env, key_provider=other, **_ctx())


def test_each_session_uses_a_fresh_dek() -> None:
    kp = _kp()
    e1 = encrypt_storage_state(_STATE, key_provider=kp, **_ctx())
    e2 = encrypt_storage_state(_STATE, key_provider=kp, **_ctx())
    assert e1 != e2  # fresh DEK + nonce each time → distinct ciphertext


def test_tampered_ciphertext_fails() -> None:
    kp = _kp()
    env = bytearray(encrypt_storage_state(_STATE, key_provider=kp, **_ctx()))
    env[-5] ^= 0xFF  # flip a byte in the base64 ciphertext region
    with pytest.raises(SessionCryptoError):
        decrypt_storage_state(bytes(env), key_provider=kp, **_ctx())


def test_bad_kek_length_rejected() -> None:
    with pytest.raises(ValueError):
        LocalKeyProvider(os.urandom(20))
