"""Google Chat webhook crypto: inbound OIDC ID-token verification + outbound SA assertion signing.

Google Chat's inbound scheme is **not** HMAC and **not** an AES/XML envelope (that's the WeCom model)
and there is **no** ``url_verification`` challenge handshake. Google signs each event POST with an
OIDC **ID token** — an RS256 JWT — placed in the ``Authorization: Bearer <jwt>`` header. Verifying it
means: check the RS256 signature against Google's published public keys, then check ``iss`` is Google,
``exp``/``iat`` are current, and ``aud`` equals the endpoint's configured audience. The ``email``
claim (the Google-side caller service account) is a Chat-specific check left to the channel, exactly
as Hermes' ``verify_http_event_request`` does after ``verify_oauth2_token`` returns.

The live adapter delegates all of this to ``google.oauth2.id_token.verify_oauth2_token``. We only have
stdlib + ``httpx`` + ``cryptography`` (adding ``google-auth`` is out of scope per the plugin rules), so
this module reimplements the same standard JWT verification directly:

* signature — ``cryptography`` RS256 (RSASSA-PKCS1-v1_5 over SHA-256);
* signing keys — Google's JWKS at ``https://www.googleapis.com/oauth2/v3/certs``, fetched and cached
  ~300s (the same TTL google-auth uses), keyed by the JWT header ``kid``;
* claims — ``iss`` ∈ {accounts.google.com, https://accounts.google.com}, ``exp``/``iat`` window, ``aud``.

The **outbound** direction needs a Service Account OAuth2 access token. Rather than pull in a Google
SDK, we mint it ourselves the standard way: build an RS256 JWT-bearer assertion signed with the SA
private key and exchange it at the token endpoint (see :mod:`client`). Both directions therefore reduce
to the two things ``cryptography`` gives us — verify an RS256 signature, and produce one.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable, Mapping

import httpx

# Google's OIDC issuer identifiers and its JWKS endpoint (JSON Web Key Set, RSA public keys by ``kid``).
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# The SA access token is obtained via the JWT-bearer grant at Google's OAuth2 token endpoint.
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
CHAT_BOT_SCOPE = "https://www.googleapis.com/auth/chat.bot"

# A small tolerance for clock drift between Google and us, matching typical OIDC verifiers.
_CLOCK_SKEW_SECONDS = 60.0

KeyResolver = Callable[[str | None], Any]


class InvalidGoogleToken(Exception):
    """The inbound bearer JWT failed verification (bad signature, wrong aud/iss, expired, …)."""


# --- base64url + JWT decoding (stdlib) ---------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url JWT segment, restoring the padding JWT strips."""
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode without padding, the JWT wire form."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_jwt(token: str) -> tuple[dict, dict, bytes, bytes]:
    """Split a compact JWT into ``(header, claims, signing_input, signature)`` — no verification yet."""
    parts = token.split(".")
    if len(parts) != 3:
        raise InvalidGoogleToken("malformed JWT (expected three dot-separated segments)")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except Exception as exc:  # noqa: BLE001 - any decode failure is an invalid token
        raise InvalidGoogleToken(f"undecodable JWT: {exc}") from exc
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    return header, claims, signing_input, signature


# --- RSA public-key handling (cryptography) ----------------------------------------------------


def rsa_public_key_from_jwk(jwk: Mapping[str, Any]) -> Any:
    """Reconstruct an RSA public key from a JWK's ``n``/``e`` (both base64url big-endian integers)."""
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
    e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
    return RSAPublicNumbers(e, n).public_key()


def _verify_rs256(public_key: Any, signing_input: bytes, signature: bytes) -> bool:
    """True iff ``signature`` is a valid RS256 (PKCS#1 v1.5 / SHA-256) signature over ``signing_input``."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
        return True
    except InvalidSignature:
        return False


# --- inbound: verify a Google-signed OIDC ID token ---------------------------------------------


def verify_google_id_token(
    token: str,
    *,
    audience: str,
    resolve_key: KeyResolver,
    now: float | None = None,
    issuers: frozenset[str] = GOOGLE_ISSUERS,
    leeway: float = _CLOCK_SKEW_SECONDS,
) -> dict:
    """Verify a Google OIDC ID token and return its claims, mirroring ``verify_oauth2_token``.

    Checks the RS256 signature (against the key ``resolve_key(kid)`` supplies), then ``iss``, the
    ``exp``/``iat`` window, and ``aud``. The Chat-side ``email`` claim is intentionally *not* checked
    here — that identity gate belongs to the channel, exactly as Hermes checks it after this returns.
    Any failure raises :class:`InvalidGoogleToken` (fail closed).
    """
    header, claims, signing_input, signature = decode_jwt(token)

    if header.get("alg") != "RS256":  # pin the algorithm — never trust the token's own ``alg`` blindly
        raise InvalidGoogleToken(f"unexpected JWT alg {header.get('alg')!r}")
    key = resolve_key(header.get("kid"))
    if key is None:
        raise InvalidGoogleToken("no signing key for JWT kid")
    if not _verify_rs256(key, signing_input, signature):
        raise InvalidGoogleToken("RS256 signature mismatch")

    current = time.time() if now is None else now
    exp = claims.get("exp")
    if exp is None or current > float(exp) + leeway:
        raise InvalidGoogleToken("token expired")
    iat = claims.get("iat")
    if iat is not None and current + leeway < float(iat):
        raise InvalidGoogleToken("token used before issued")
    if str(claims.get("iss") or "") not in issuers:
        raise InvalidGoogleToken("unexpected issuer")
    if not audience or claims.get("aud") != audience:
        raise InvalidGoogleToken("audience mismatch")

    return claims


class GoogleCertsCache:
    """Google's OIDC signing keys (JWKS), fetched on demand and cached ~300s, keyed by ``kid``.

    This is the production key source for :func:`verify_google_id_token`; tests inject a static
    resolver instead. The fetch is a short blocking HTTPS GET — the same shape google-auth performs —
    and only runs on a cache miss/expiry, so it does not sit on the hot path of every event. Any fetch
    error leaves the cache untouched and yields ``None``, so verification fails closed.
    """

    def __init__(
        self, certs_url: str = GOOGLE_CERTS_URL, *, http_client: httpx.Client | None = None, ttl: float = 300.0
    ) -> None:
        self._url = certs_url
        self._client = http_client
        self._owns_client = http_client is None
        self._ttl = ttl
        self._keys: dict[str, Any] = {}
        self._expiry = 0.0

    def get_key(self, kid: str | None) -> Any:
        now = time.monotonic()
        # Refresh when the cache is empty, expired, or missing the kid the token was signed with
        # (a key rotation shows up as a miss, which google-auth also treats as "refetch and retry").
        if not self._keys or now >= self._expiry or (kid and kid not in self._keys):
            self._refresh()
        return self._keys.get(kid) if kid else None

    def _refresh(self) -> None:
        client = self._client if self._client is not None else httpx.Client(timeout=10.0)
        try:
            data = client.get(self._url).json()
        except Exception:  # noqa: BLE001 - keep the old cache on a transient fetch failure
            return
        finally:
            if self._owns_client:
                client.close()
        keys: dict[str, Any] = {}
        for jwk in (data or {}).get("keys", []):
            kid = jwk.get("kid")
            if not kid or jwk.get("kty") != "RSA":
                continue
            try:
                keys[str(kid)] = rsa_public_key_from_jwk(jwk)
            except Exception:  # noqa: BLE001 - skip a malformed key, keep the rest
                continue
        if keys:
            self._keys = keys
            self._expiry = time.monotonic() + self._ttl

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None


# --- outbound: sign a Service Account JWT-bearer assertion -------------------------------------


def sign_service_account_assertion(
    *,
    client_email: str,
    private_key_pem: str,
    private_key_id: str = "",
    scope: str = CHAT_BOT_SCOPE,
    token_uri: str = GOOGLE_TOKEN_URI,
    now: float | None = None,
    ttl: int = 3600,
) -> str:
    """Build the RS256 JWT-bearer assertion the token endpoint exchanges for an SA access token.

    Standard SA flow: ``iss`` = the SA email, ``aud`` = the token endpoint, ``scope`` = the granted
    scope (``chat.bot`` is enough for every send/patch/delete), signed with the SA private key.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    issued = int(time.time() if now is None else now)
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if private_key_id:
        header["kid"] = private_key_id
    claims = {
        "iss": client_email,
        "scope": scope,
        "aud": token_uri,
        "iat": issued,
        "exp": issued + ttl,
    }
    signing_input = (
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64url_encode(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    ).encode("ascii")

    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return signing_input.decode("ascii") + "." + _b64url_encode(signature)
