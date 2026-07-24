"""Unified DLP Gateway (design §11.1, §11.3).

Every outbound surface routes through one gateway rather than each module choosing whether to scrub
(design §11.1). :meth:`DLPGateway.scrub` takes a ``surface`` (model request, transcript, artifact, log,
audit, telemetry, api, crash report) and a payload, and returns a :class:`DLPDecision`:

* it **fails closed on a canary**: if a registered secret fingerprint appears anywhere in the payload,
  the egress is blocked and the response actions of §11.3 fire (via ``on_secret_blocked``) — end the
  auth lease, invalidate the capability, mark the managed session non-reusable — and a
  ``dlp.secret_blocked`` audit event is emitted that contains only the one-way fingerprint, never the
  secret (§11.3 steps 1–5);
* it **refuses to serialize** a ``SecretValue`` / ``ResolvedCredentials`` / ``CredentialCapability``
  (design §11.2) — their presence blocks the egress;
* otherwise it applies the format-based redactions (headers, URLs, sensitive keys, identifiers).

The gateway is defense-in-depth, not the primary boundary (§11.4): the process/permission isolation is
what actually keeps secrets out. A regex pass is not a licence to relax that isolation.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

from tabvis.authentication.models import CredentialCapability, ResolvedCredentials
from tabvis.authentication.secrets import BufferSecretValue, SecretValue
from tabvis.dlp import canary
from tabvis.dlp.text import mask_identifiers, redact_headers, redact_mapping
from tabvis.dlp.url import clean_url

# Egress surfaces (design §11.1).
SURFACES = (
    "model_request",
    "transcript",
    "artifact",
    "log",
    "audit",
    "telemetry",
    "api",
    "crash_report",
)

_URL_KEYS = {"url", "uri", "href", "location", "top_level_url", "frame_url"}


class DLPBlockEvent(BaseModel):
    """The ``dlp.secret_blocked`` audit event — one-way fingerprint only, never canary content (§11.3)."""

    model_config = ConfigDict(extra="forbid")

    event: str
    surface: str
    fingerprint: str
    timestamp: str


class DLPDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    blocked: bool
    payload: object = None
    fingerprint: str | None = None


class DLPGateway:
    def __init__(self, *, on_secret_blocked: Callable[[DLPBlockEvent], None] | None = None) -> None:
        self._on_blocked = on_secret_blocked

    def scrub(self, surface: str, payload: object) -> DLPDecision:
        # 1. forbidden-object check: a secret-bearing object must never be serialized outward (§11.2).
        if _contains_forbidden_object(payload):
            return self._block(surface, fingerprint="forbidden-object")

        # 2. canary value scan across every string in the payload (§11.3).
        for text in _iter_strings(payload):
            fp = canary.scan_text(text)
            if fp is not None:
                return self._block(surface, fingerprint=fp)

        # 3. format-based redaction.
        return DLPDecision(surface=surface, blocked=False, payload=_deep_clean(payload))

    def _block(self, surface: str, *, fingerprint: str) -> DLPDecision:
        if self._on_blocked is not None:
            event = DLPBlockEvent(
                event="dlp.secret_blocked",
                surface=surface,
                fingerprint=fingerprint,
                timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            try:
                self._on_blocked(event)
            except Exception:  # noqa: BLE001 - a broken hook must not turn a block into a leak
                pass
        return DLPDecision(surface=surface, blocked=True, payload=None, fingerprint=fingerprint)


# --------------------------------------------------------------------------- helpers


def _contains_forbidden_object(payload: object) -> bool:
    if isinstance(payload, (SecretValue, BufferSecretValue, ResolvedCredentials, CredentialCapability)):
        return True
    if isinstance(payload, dict):
        return any(_contains_forbidden_object(v) for v in payload.values()) or any(
            _contains_forbidden_object(k) for k in payload.keys()
        )
    if isinstance(payload, (list, tuple, set)):
        return any(_contains_forbidden_object(v) for v in payload)
    return False


def _iter_strings(payload: object):
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str):
                yield k
            yield from _iter_strings(v)
    elif isinstance(payload, (list, tuple, set)):
        for v in payload:
            yield from _iter_strings(v)


def _deep_clean(payload: object) -> object:
    if isinstance(payload, str):
        return mask_identifiers(payload)
    if isinstance(payload, dict):
        # header-shaped dicts: redact credential headers by name
        cleaned = redact_headers(payload) if _looks_like_headers(payload) else redact_mapping(payload)
        out: dict = {}
        for key, value in cleaned.items():
            if isinstance(key, str) and key.lower() in _URL_KEYS and isinstance(value, str):
                out[key] = clean_url(value)
            else:
                out[key] = _deep_clean(value)
        return out
    if isinstance(payload, list):
        return [_deep_clean(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(_deep_clean(v) for v in payload)
    return payload


def _looks_like_headers(payload: dict) -> bool:
    keys = {k.lower() for k in payload.keys() if isinstance(k, str)}
    return bool(keys & {"cookie", "set-cookie", "authorization", "proxy-authorization"})
