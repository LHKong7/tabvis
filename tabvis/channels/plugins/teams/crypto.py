"""Microsoft Teams (Bot Framework) inbound auth: JWT Bearer validation (RS256 via JWKS).

Teams is *not* an HMAC-signed webhook like Slack or Feishu. Bot Framework authenticates the
channel → bot direction with a signed JSON Web Token in the ``Authorization: Bearer <jwt>`` request
header. There is no shared secret, no ``url_verification`` challenge, no AES envelope — so replicating
the platform's verification means doing the JWT dance the Bot Framework SDK does internally:

* split the compact JWS (``header.payload.signature``, base64url) and read the ``kid``;
* select that signing key from the Bot Framework JWKS — normally fetched and cached from
  ``https://login.botframework.com/v1/.well-known/openidconfiguration`` (the channel → bot metadata);
* verify the **RS256** signature with the selected RSA public key;
* validate the claims: ``aud`` == our bot's client id, ``iss`` == ``https://api.botframework.com``,
  the token is unexpired, and — the Bot Framework anti-spoofing check — the token's ``serviceurl``
  claim matches the ``serviceUrl`` on the activity being delivered.

RS256 verification needs the ``cryptography`` package, imported lazily like the Feishu AES path so the
base64url/JSON structural decode (useful for inspecting a token) works with only the standard library.
The JWKS itself is injected/refreshed rather than fetched inside this synchronous check — see
``TeamsChannel.refresh_signing_keys`` and the module note in ``channel.py``.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Mapping

# Bot Framework signs channel → bot tokens with this fixed issuer (public Azure cloud).
BOT_FRAMEWORK_ISSUER = "https://api.botframework.com"
# Where the SDK fetches the signing keys + metadata for the channel → bot direction.
OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"


class JwtError(Exception):
    """A Bot Framework bearer token failed structural, signature, or claim validation."""


def b64url_decode(segment: str) -> bytes:
    """Decode a base64url JWS segment, restoring the stripped ``=`` padding."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def b64url_encode(data: bytes) -> str:
    """Base64url without padding — the JWS/JWK on-the-wire form."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_segments(token: str) -> tuple[dict, dict, bytes, bytes]:
    """Split a compact JWS into ``(header, claims, signing_input, signature)`` without verifying it."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("token is not a well-formed JWS (need three dot-separated segments)")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
        signature = b64url_decode(sig_b64)
    except Exception as exc:  # noqa: BLE001
        raise JwtError(f"token segments are not valid base64url/JSON: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise JwtError("token header and claims must both be JSON objects")
    # The signature covers the ASCII bytes of "header_b64.payload_b64" exactly as received.
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    return header, claims, signing_input, signature


class SigningKeyStore:
    """The set of Bot Framework signing keys, keyed by ``kid`` (a cached JWKS in production)."""

    def __init__(self, keys: Mapping[str, dict] | None = None) -> None:
        self._keys: dict[str, dict] = dict(keys or {})

    def load_jwks(self, document: Mapping[str, Any]) -> None:
        """Merge the ``keys`` of a JWKS document into the store (kid → JWK)."""
        for jwk in (document or {}).get("keys", []) or []:
            kid = jwk.get("kid")
            if kid:
                self._keys[kid] = jwk

    def get(self, kid: str) -> dict | None:
        return self._keys.get(kid)

    @property
    def is_empty(self) -> bool:
        return not self._keys

    def __len__(self) -> int:
        return len(self._keys)


def _rsa_public_key(jwk: Mapping[str, Any]):
    """Build an RSA public key from a JWK's ``n``/``e``; lazy-imports ``cryptography``."""
    try:
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    except ImportError as exc:  # optional extra, like the Feishu AES path
        raise RuntimeError(
            "Teams inbound JWT validation needs the 'cryptography' package. Install it with "
            "`uv sync --extra teams` (RS256 verification of the Bot Framework bearer token)."
        ) from exc
    try:
        n = int.from_bytes(b64url_decode(str(jwk["n"])), "big")
        e = int.from_bytes(b64url_decode(str(jwk["e"])), "big")
    except (KeyError, ValueError, TypeError) as exc:
        raise JwtError(f"signing key is not a usable RSA JWK: {exc}") from exc
    return RSAPublicNumbers(e, n).public_key()


def verify_rs256(signing_input: bytes, signature: bytes, jwk: Mapping[str, Any]) -> None:
    """Verify an RS256 signature against a JWK; raise :class:`JwtError` if it does not check out."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = _rsa_public_key(jwk)
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise JwtError("RS256 signature does not verify against the selected Bot Framework key") from exc


def validate_bearer(
    token: str,
    *,
    key_store: SigningKeyStore,
    audience: str,
    issuer: str = BOT_FRAMEWORK_ISSUER,
    service_url: str | None = None,
    now: float | None = None,
    leeway: float = 300.0,
) -> dict:
    """Full Bot Framework bearer validation. Returns the claims on success, else raises ``JwtError``.

    Mirrors what the ``microsoft-teams-apps`` SDK does before it dispatches an activity: RS256 signature
    against the JWKS, then audience / issuer / expiry, then the ``serviceurl`` anti-spoofing cross-check.
    """
    header, claims, signing_input, signature = decode_segments(token)

    if header.get("alg") != "RS256":
        raise JwtError(f"unexpected JWT alg {header.get('alg')!r}; Bot Framework signs with RS256")
    kid = header.get("kid")
    if not kid:
        raise JwtError("JWT header has no 'kid' to select a signing key")
    jwk = key_store.get(str(kid))
    if jwk is None:
        # No matching key: either an unknown/rotated kid or an empty (never-refreshed) key store.
        raise JwtError(f"no Bot Framework signing key for kid {kid!r}")
    verify_rs256(signing_input, signature, jwk)

    aud = claims.get("aud")
    aud_ok = aud == audience or (isinstance(aud, (list, tuple)) and audience in aud)
    if not aud_ok:
        raise JwtError("token audience does not match the configured Teams client id")
    if issuer and claims.get("iss") != issuer:
        raise JwtError(f"unexpected token issuer {claims.get('iss')!r}")

    clock = time.time() if now is None else now
    exp = claims.get("exp")
    if exp is None or clock > float(exp) + leeway:
        raise JwtError("token is expired")
    nbf = claims.get("nbf")
    if nbf is not None and clock + leeway < float(nbf):
        raise JwtError("token is not yet valid (nbf in the future)")

    # Anti-spoofing: the token binds the serviceUrl it was minted for; it must match the activity's.
    claim_service_url = claims.get("serviceurl") or claims.get("serviceUrl")
    if claim_service_url and service_url and str(claim_service_url).rstrip("/") != service_url.rstrip("/"):
        raise JwtError("token serviceUrl claim does not match the activity serviceUrl")
    return claims
