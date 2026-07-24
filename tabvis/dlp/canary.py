"""DLP secret canary (design §11.3).

Every time a secret is resolved in the trusted domain, the DLP layer registers an *irreversible*
fingerprint of it. The fingerprint — not the secret — is what the DLP Gateway scans every outbound
surface for (model requests, transcripts, artifacts, logs, telemetry, error responses, temp files).
A hit means a secret escaped its domain, which MUST hard-fail the egress and raise a security event
(design §11.3 steps 1–5).

The fingerprint is a keyed BLAKE2 digest, so:

* it is one-way — the registry never stores, and cannot reconstruct, the plaintext;
* it is keyed with a per-process random salt, so a registered fingerprint is meaningless outside this
  process and is itself safe to log in a ``dlp.secret_blocked`` event (design §11.3 step 4: the event
  must NOT contain canary content).

This is the fingerprint/registry core (Phase 0). Wiring every egress through it, and the value-matching
scan over free text, land with the full DLP Gateway in Phase 5 (design §11.1); :func:`scan_text` here
provides the exact-substring detector those egress points will call.
"""

from __future__ import annotations

import hashlib
import os
import threading

_DIGEST_SIZE = 16
# Per-process salt: keys the fingerprints so they can't be correlated across processes or precomputed.
_SALT = os.urandom(16)
_lock = threading.RLock()
# Registered fingerprints -> an opaque short tag for the audit event (never the secret).
_registry: dict[str, str] = {}
# Byte-lengths of registered secrets, so :func:`scan_text` can slide a window of each known length over
# arbitrary text and fingerprint each window — real substring detection without ever storing plaintext.
_lengths: set[int] = set()
# Minimum secret length we fingerprint. Very short secrets would produce too many false positives when
# substring-scanning arbitrary text, so they are skipped (defense-in-depth only; §11.4).
_MIN_CANARY_LEN = 6


def _fingerprint(raw: bytes) -> str:
    return hashlib.blake2b(raw, digest_size=_DIGEST_SIZE, salt=_SALT[:16]).hexdigest()


def register(raw: bytes, *, tag: str) -> str | None:
    """Register the irreversible fingerprint of a just-resolved secret. Returns the fingerprint.

    ``tag`` is a non-sensitive label (e.g. ``"password:profile_x"``) used only in the eventual audit
    event. Returns None (registers nothing) for secrets below the length floor.
    """
    if len(raw) < _MIN_CANARY_LEN:
        return None
    fp = _fingerprint(raw)
    with _lock:
        _registry[fp] = tag
        _lengths.add(len(raw))
    return fp


def is_registered(raw: bytes) -> bool:
    """Whether ``raw`` matches a registered secret fingerprint."""
    if len(raw) < _MIN_CANARY_LEN:
        return False
    with _lock:
        return _fingerprint(raw) in _registry


def scan_text(text: str) -> str | None:
    """Scan free text for an embedded registered secret; return the offending fingerprint or None.

    Slides a window of each registered secret length over the text's bytes and fingerprints each
    window, so a secret pasted *inside* a larger string (a URL, a DOM blob, a log line) is caught — not
    just whole-string equality. The design is explicit (§11.4) that this value scan is defense-in-depth,
    not the primary boundary, since unknown-format secrets can't be recognized; the process/permission
    isolation is what actually keeps secrets out.
    """
    if not text:
        return None
    raw = text.encode("utf-8", "ignore")
    with _lock:
        registry = dict(_registry)
        lengths = sorted(_lengths)
    n = len(raw)
    for length in lengths:
        if length > n:
            continue
        for start in range(0, n - length + 1):
            fp = _fingerprint(raw[start : start + length])
            if fp in registry:
                return fp
    return None


def scan_tokens(tokens: list[str]) -> str | None:
    """Scan a list of candidate tokens (e.g. header values, query values) for a registered secret.

    Egress points that have already split a payload into fields pass the field values here; this is the
    reliable detector (whole-value equality against the one-way registry). Returns the offending
    fingerprint or None.
    """
    for token in tokens:
        if token and is_registered(token.encode("utf-8", "ignore")):
            return _fingerprint(token.encode("utf-8", "ignore"))
    return None


def clear() -> None:
    """Drop all registered fingerprints (process shutdown / test reset)."""
    with _lock:
        _registry.clear()
        _lengths.clear()


def registered_count() -> int:
    with _lock:
        return len(_registry)
