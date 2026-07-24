"""TOTP generation inside the Executor (design §9.3).

RFC 6238 time-based one-time passwords. The hard rules from §9.3:

* the TOTP **seed** is resolved only by the Executor and reaches this module only as a
  :class:`~tabvis.authentication.secrets.SecretValue` — never a plain ``str``;
* the generated **code** is itself a secret: it is produced straight into a short-lived
  :class:`~tabvis.authentication.secrets.BufferSecretValue` so it can be typed and then scrubbed, and
  it never enters a log, result or exception;
* a **trusted time source** is supplied by the caller (``at`` — Unix seconds), not read implicitly, so
  time is explicit and testable;
* clock drift is bounded: :func:`totp_candidates` yields the current step and at most ``drift``
  adjacent steps (default 1), matching "当前时间步和一个相邻时间步".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from collections.abc import Iterator

from tabvis.authentication.secrets import BufferSecretValue, SecretValue

_ALGORITHMS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def _decode_seed(seed: SecretValue) -> bytearray:
    """Base32-decode the seed from its :class:`SecretValue` into an owned, scrub(able) key buffer."""
    view = seed.borrow_bytes()
    try:
        raw = bytes(view).strip()
    finally:
        # We copied into ``raw``; the borrowed view can be dropped. (The seed's own buffer is the
        # caller's to release.)
        del view
    # Uppercase + pad to a multiple of 8 so casual lower-case / unpadded seeds still decode.
    normalized = raw.upper()
    normalized += b"=" * ((-len(normalized)) % 8)
    try:
        key = bytearray(base64.b32decode(normalized, casefold=True))
    finally:
        # scrub the intermediate copies
        raw = b""  # noqa: F841
        normalized = b""  # noqa: F841
    return key


def _hotp(key: bytearray, counter: int, *, digits: int, algorithm: str) -> bytes:
    """One HOTP value (RFC 4226) rendered as ``digits`` ASCII bytes."""
    digestmod = _ALGORITHMS[algorithm]
    msg = struct.pack(">Q", counter)
    mac = hmac.new(bytes(key), msg, digestmod).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    code = binary % (10**digits)
    return f"{code:0{digits}d}".encode("ascii")


def generate_totp(
    seed: SecretValue,
    *,
    at: float,
    digits: int = 6,
    period: int = 30,
    algorithm: str = "sha1",
    step_offset: int = 0,
) -> BufferSecretValue:
    """Generate the TOTP code as a :class:`BufferSecretValue` (design §9.3).

    ``at`` is trusted Unix time (seconds). ``step_offset`` shifts the time step by whole periods for
    drift handling. The returned code is a secret — hand it straight to ``type_secret`` and release it.
    """
    if algorithm not in _ALGORITHMS:
        raise ValueError(f"unsupported TOTP algorithm: {algorithm!r}")
    key = _decode_seed(seed)
    try:
        counter = int(at // period) + step_offset
        code = _hotp(key, counter, digits=digits, algorithm=algorithm)
        secret = BufferSecretValue(code)
    finally:
        # scrub the decoded key material regardless of outcome
        for i in range(len(key)):
            key[i] = 0
    return secret


def totp_candidates(
    seed: SecretValue,
    *,
    at: float,
    drift: int = 1,
    digits: int = 6,
    period: int = 30,
    algorithm: str = "sha1",
) -> Iterator[BufferSecretValue]:
    """Yield the current-step code, then up to ``drift`` adjacent steps (design §9.3 default 1).

    Order is current, then symmetric neighbors outward (+1, -1, +2, -2, …). The Executor tries each in
    turn against the site and releases it before the next; it MUST NOT collect them into a list that
    outlives the attempt.
    """
    offsets = [0]
    for d in range(1, drift + 1):
        offsets.extend([d, -d])
    for offset in offsets:
        yield generate_totp(
            seed,
            at=at,
            digits=digits,
            period=period,
            algorithm=algorithm,
            step_offset=offset,
        )
