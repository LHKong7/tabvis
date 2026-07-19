"""String/content hashing helpers

Three functions:

- :func:`djb2_hash` — the djb2 non-cryptographic hash, returning a SIGNED 32-bit int
  (exactly mirroring the JS ``| 0`` truncation so cache keys stay byte-stable across
  runtimes).
- :func:`hash_content` — content hashing for change detection.
- :func:`hash_pair` — hash two strings without a concatenated temp.

Bun branch: the TS file fast-paths ``Bun.hash`` (wyhash) when running under Bun, falling
back to Node ``crypto`` SHA-256 otherwise. There is no Bun under Python, so this implementation
takes the **Node/SHA-256 branch unconditionally** — the deterministic, on-disk-stable
path the TS docstrings call out as the cross-runtime-safe fallback. The Bun-specific
seed-chaining is therefore not reachable here (its outputs were never portable anyway,
being wyhash). ``hash_pair`` keeps the NUL (``\0``) separator the Node branch uses.

Casing: snake_case identifiers; plain ``str``/``int`` in and out — no wire-key dicts.
The module name ``tabvis.utils.hash`` does NOT shadow the stdlib ``hashlib`` used below.
"""

from __future__ import annotations

import hashlib

_INT32_MASK = 0xFFFFFFFF
_INT32_SIGN = 0x80000000
_INT32_RANGE = 0x100000000


def djb2_hash(string: str) -> int:
    """djb2 string hash returning a signed 32-bit integer.

    Fast non-cryptographic hash, deterministic across runtimes (unlike ``Bun.hash``
    which uses wyhash). Use as a fallback when a runtime-stable value is needed, e.g.
    cache directory names that must survive runtime upgrades.

    The hash is folded to a signed 32-bit int on every step to mirror the JS
    ``((hash << 5) - hash + charCode) | 0`` truncation exactly. ``charCodeAt`` returns
    a UTF-16 code unit, so this iterates UTF-16 code units (not Unicode code points) to
    stay byte-identical for astral-plane characters.
    """
    hash_value = 0
    # Iterate UTF-16 code units to match JS ``String.prototype.charCodeAt``.
    utf16 = string.encode("utf-16-le")
    for i in range(0, len(utf16), 2):
        char_code = utf16[i] | (utf16[i + 1] << 8)
        # ((hash << 5) - hash + charCode) | 0  →  emulate signed 32-bit wraparound.
        hash_value = ((hash_value << 5) - hash_value + char_code) & _INT32_MASK
    # Reinterpret the unsigned 32-bit result as signed (JS ``| 0`` semantics).
    if hash_value >= _INT32_SIGN:
        hash_value -= _INT32_RANGE
    return hash_value


def hash_content(content: str) -> str:
    """Hash arbitrary content for change detection.

    Returns the hex SHA-256 digest of the UTF-8 bytes (the Node branch of the TS
    source; the ``Bun.hash`` fast path is not reachable under Python). Collision
    resistance is more than sufficient for diff detection — not crypto-safe by intent,
    but SHA-256 here is simply the stable cross-runtime fallback.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def hash_pair(a: str, b: str) -> str:
    """Hash two strings without allocating a concatenated temp string.

    Node-branch parity: incremental SHA-256 over ``a``, a NUL (``\\0``) separator, then
    ``b`` — so ``("ts", "code")`` and ``("tsc", "ode")`` hash differently. Returns the
    hex digest.
    """
    hasher = hashlib.sha256()
    hasher.update(a.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(b.encode("utf-8"))
    return hasher.hexdigest()
