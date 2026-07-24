"""Origin & frame policy primitives (design §8).

Only the pure, deterministic pieces of the Policy Engine live here — origin canonicalization and the
frame-chain check. The stateful checks (usage counts, rate limits, lease exclusivity, provider health,
approval) belong to the Broker and land in later phases; this module is the part that must be
bullet-proof against homograph / trailing-dot / userinfo / port / IDN spoofing (design §2.1 "Origin
欺骗", §8.1), so it is isolated and exhaustively unit-tested.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Default ports we normalize away so ``https://x`` and ``https://x:443`` compare equal.
_DEFAULT_PORTS = {"https": 443, "http": 80}


class OriginError(ValueError):
    """A URL could not be reduced to a valid, allowed https Origin."""


def canonicalize_origin(url: str) -> str:
    """Reduce a URL to a canonical ``https://host[:port]`` Origin, or raise :class:`OriginError`.

    Enforces design §8.1:

    * a real parser is used, never string prefixing;
    * only ``https`` is accepted;
    * host is lower-cased and IDN is converted to ASCII/Punycode before comparison;
    * the default 443 port is normalized away;
    * URL userinfo (``user:pass@``) is rejected outright;
    * path / query / fragment are ignored (Origin only);
    * a trailing dot on the host (``example.com.``) is stripped so it can't dodge an exact match while
      resolving to the same site.
    """
    if not isinstance(url, str) or not url.strip():
        raise OriginError("empty url")
    parts = urlsplit(url.strip())

    if parts.scheme != "https":
        raise OriginError(f"scheme must be https, got {parts.scheme!r}")
    if parts.username or parts.password:
        raise OriginError("url userinfo is not allowed")

    host = parts.hostname  # already lower-cased and userinfo/port-stripped by urlsplit
    if not host:
        raise OriginError("missing host")
    # Strip a single trailing dot (fully-qualified form) so example.com. == example.com.
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise OriginError("missing host")
    # IDN → Punycode. urlsplit already lower-cases ASCII; encode non-ASCII deterministically so a
    # homograph in a different Unicode form can't slip past an exact-string compare.
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise OriginError("invalid international domain name") from exc
    host = host.lower()

    try:
        port = parts.port
    except ValueError as exc:  # non-numeric / out-of-range port
        raise OriginError("invalid port") from exc

    if port is None or port == _DEFAULT_PORTS.get(parts.scheme):
        return f"https://{host}"
    return f"https://{host}:{port}"


def origin_matches(candidate: str, allowed: list[str]) -> bool:
    """Whether ``candidate`` canonicalizes to one of the canonicalized ``allowed`` Origins.

    Exact Origin match only — no path/subdomain/implicit wildcard (design §5.4). Anything that fails
    to canonicalize (bad scheme, userinfo, port) is a non-match, never a crash, so a hostile URL can't
    take out the check by raising.
    """
    try:
        canon = canonicalize_origin(candidate)
    except OriginError:
        return False
    allowed_set = set()
    for entry in allowed:
        try:
            allowed_set.add(canonicalize_origin(entry))
        except OriginError:
            continue
    return canon in allowed_set


def frame_chain_authorized(
    frame_origin: str,
    ancestor_frame_origins: list[str],
    allowed_frame_origins: list[str],
) -> bool:
    """Whether the input frame *and every cross-origin ancestor* are in the authorized set (design §8.2).

    The field's own frame Origin plus all ancestor frame Origins MUST each canonicalize into
    ``allowed_frame_origins``. A single unauthorized link anywhere in the chain fails the whole check —
    this is what stops a malicious outer/inner iframe from harvesting the password.
    """
    for origin in [frame_origin, *ancestor_frame_origins]:
        if not origin_matches(origin, allowed_frame_origins):
            return False
    return True
