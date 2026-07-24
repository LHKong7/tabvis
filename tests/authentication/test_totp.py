"""TOTP generation (design §9.3, §16.1).

Values are checked against the RFC 6238 published test vectors (seed "12345678901234567890" =
base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ") so the algorithm is verifiably correct.
"""

from __future__ import annotations

import pytest

from tabvis.authentication.secrets import SecretLeakError, secret_from_str
from tabvis.authentication.totp import generate_totp, totp_candidates

# base32 of the 20-byte ASCII seed "12345678901234567890" (RFC 6238 SHA1 vector).
_SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def _code(secret) -> str:
    return bytes(secret.borrow_bytes()).decode()


def test_rfc6238_vectors_sha1() -> None:
    seed = secret_from_str(_SEED_B32)
    # RFC 6238 Appendix B, SHA1, 8 digits.
    cases = {59: "94287082", 1111111109: "07081804", 1234567890: "89005924"}
    for at, expected in cases.items():
        code = generate_totp(seed, at=at, digits=8)
        assert _code(code) == expected
        code.release()


def test_default_is_six_digits() -> None:
    seed = secret_from_str(_SEED_B32)
    code = generate_totp(seed, at=59)
    assert len(_code(code)) == 6
    code.release()


def test_step_offset_changes_code() -> None:
    seed = secret_from_str(_SEED_B32)
    c0 = generate_totp(seed, at=59, step_offset=0)
    c1 = generate_totp(seed, at=59, step_offset=1)
    assert _code(c0) != _code(c1)
    c0.release()
    c1.release()


def test_code_is_a_secret_value() -> None:
    seed = secret_from_str(_SEED_B32)
    code = generate_totp(seed, at=59)
    with pytest.raises(SecretLeakError):
        str(code)  # the OTP is itself a secret (§9.3)
    code.release()


def test_candidates_current_then_neighbors() -> None:
    seed = secret_from_str(_SEED_B32)
    got = []
    for c in totp_candidates(seed, at=1000, drift=1):
        got.append(_code(c))
        c.release()
    # current, +1 step, -1 step
    assert got[0] == _code_at(seed, 1000, 0)
    assert got[1] == _code_at(seed, 1000, 1)
    assert got[2] == _code_at(seed, 1000, -1)
    assert len(got) == 3


def _code_at(seed, at, offset) -> str:
    c = generate_totp(seed, at=at, step_offset=offset)
    out = _code(c)
    c.release()
    return out


def test_bad_algorithm_raises() -> None:
    seed = secret_from_str(_SEED_B32)
    with pytest.raises(ValueError):
        generate_totp(seed, at=59, algorithm="md5")
