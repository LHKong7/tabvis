"""Resource normalization + glob matching for the permission policy engine (PP-1).

``docs/permission-policy-engine_v1.md`` §5.2: a resource is **normalized before it is matched** — a raw
string path never participates in a prefix test. Every resource lives in a logical namespace
(``workspace:`` / ``session:`` / ``artifact:`` / ``config:`` / ``url:``); the namespace is matched
exactly and the remainder is glob-matched (``*`` within a segment, ``**`` across segments).

Two safety-relevant normalizations happen here (the rest — real ``realpath`` + symlink resolution — is
an adapter concern in PP-7, since it needs the live filesystem):

* **Path traversal** — ``..`` that climbs above the namespace root sets ``escaped=True``; an escaped
  resource matches no namespaced pattern, so it falls through to the mode's (deny/ask) fallback rather
  than sneaking under a ``workspace:**`` allow.
* **URL canonicalization** — scheme + host are lowercased and the host is IDN-encoded (punycode), so
  ``HTTPS://ExAmPlE.com`` and ``https://xn--…`` compare equal.

Pure module: no filesystem, network, or global state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Namespaces that are recognized as an explicit ``<ns>:<rest>`` prefix. Anything else is treated as the
# wildcard namespace ``*`` (a bare pattern like ``**`` that matches every namespace).
KNOWN_NAMESPACES: frozenset[str] = frozenset(
    {"workspace", "session", "artifact", "config", "url", "secret", "fs", "agent"}
)

_ANY = "*"


@dataclass(frozen=True)
class ResourceRef:
    """A parsed resource: its ``namespace`` and normalized ``path``, plus the ``escaped`` flag."""

    namespace: str
    path: str
    escaped: bool = False
    raw: str = ""


def _normalize_rel_path(rest: str) -> tuple[str, bool]:
    """Collapse ``.``/``..``/duplicate slashes in a namespace-relative path.

    Returns ``(normalized, escaped)`` where ``escaped`` is True if the path climbs above its root.
    Never touches the real filesystem — this is lexical normalization only.
    """
    rest = rest.replace("\\", "/").lstrip("/")
    out: list[str] = []
    escaped = False
    for seg in rest.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            else:
                escaped = True
            continue
        out.append(seg)
    return "/".join(out), escaped


def _normalize_url(rest: str) -> tuple[str, bool]:
    """Canonicalize a URL resource to ``scheme://host[:port]/path`` (lowercased scheme+host, IDN)."""
    # ``url:`` patterns often carry globs (``https://**``) that break urlsplit's host parsing; if a
    # wildcard is present in the authority, keep the string lexically lowercased and don't over-parse.
    lowered = rest.strip()
    try:
        parts = urlsplit(lowered)
    except ValueError:
        return lowered.lower(), False
    if not parts.scheme or not parts.netloc or "*" in parts.netloc:
        # Pattern-ish or malformed: fold case on scheme/host lexically, leave globs intact.
        return lowered if "*" in lowered else lowered.lower(), False

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        host = host.encode("idna").decode("ascii") if host else host
    except (UnicodeError, ValueError):
        pass  # leave the lowercased host as-is if it isn't IDN-encodable
    port = f":{parts.port}" if parts.port is not None else ""
    path = parts.path or ""
    return f"{scheme}://{host}{port}{path}", False


def normalize_resource(resource: str) -> ResourceRef:
    """Parse ``resource`` into a :class:`ResourceRef`, normalizing per namespace.

    A leading ``<ns>:`` selects the namespace when ``<ns>`` is known; otherwise the whole string is
    the path in the wildcard namespace ``*`` (so a bare ``**`` pattern matches everything).
    """
    ns = _ANY
    rest = resource
    if ":" in resource:
        head, tail = resource.split(":", 1)
        if head in KNOWN_NAMESPACES:
            ns, rest = head, tail

    if ns == "url":
        path, escaped = _normalize_url(rest)
    elif ns in ("workspace", "session", "config"):
        path, escaped = _normalize_rel_path(rest)
    else:  # artifact, secret, or wildcard namespace: keep lexically, just trim slashes
        path, escaped = rest.lstrip("/"), False
    return ResourceRef(namespace=ns, path=path, escaped=escaped, raw=resource)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex. ``**`` matches across ``/``; ``*`` matches within a segment."""
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(pattern_path: str, concrete_path: str) -> bool:
    """True if the normalized ``concrete_path`` matches the glob ``pattern_path``."""
    return _glob_to_regex(pattern_path).match(concrete_path) is not None


def resource_matches(pattern: str, concrete: ResourceRef) -> bool:
    """Does the rule resource ``pattern`` cover the concrete :class:`ResourceRef`?

    An ``escaped`` concrete resource (path that climbed above its root) matches no namespaced
    pattern — it can only be handled by the mode fallback, never silently allowed.
    """
    pat = normalize_resource(pattern)
    if concrete.escaped:
        return False
    if pat.namespace != _ANY and pat.namespace != concrete.namespace:
        return False
    return glob_match(pat.path, concrete.path)
