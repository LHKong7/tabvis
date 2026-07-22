"""Deterministic evidence sanitizer for Agent Browser Memory (Resume Plus, design §9.2 / §13.4).

Before any evidence (a visited URL, a page title, a typed-text field, an on-disk artifact path) can
be handed to the memory consolidator, it passes through this module. Everything here is **pure and
deterministic** — no model, no I/O, no global state — so it is cheap, testable, and auditable, and it
runs as the last line of defense regardless of what upstream capture did.

What it guarantees (design §9.2):

1. excluded origins and unsupported schemes (``data:``/``javascript:``/browser-internal/``file:``) are
   dropped outright;
2. URL userinfo (``user:pass@``) and fragments (``#...``) are removed;
3. query *values* are dropped by default — only explicitly allowlisted, non-sensitive keys survive,
   and even those are re-checked against the sensitive-key patterns;
4. keys that look like a token / auth / session / code / password / signature are removed entirely;
5. typed text is reduced to length + coarse type metadata, never the characters;
6. titles / snippets are truncated to fixed limits and Unicode-normalized;
7. every returned item is labelled with a trust class so the consolidator can enforce §9.3;
8. any path that escapes the resolved session/artifact root is rejected.

The excluded-origin matcher (§13.4) accepts exact origins (``https://bank.example``) and safe host
wildcards (``https://*.health.example`` or bare ``*.health.example``).
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

# Version of the sanitizer's rules. Part of the consolidation job idempotency key (design §10.5): a
# rules change here must force re-extraction rather than silently reusing an old CandidateSet.
SANITIZER_VERSION = "1"

# Trust classes (design §9.3). Web-derived content can never mint a durable user fact / instruction.
TrustClass = Literal["user_authored", "assistant", "web_content", "browser_runtime", "system"]

# Coarse type label attached to redacted typed input, so a digest can note "a password-shaped field
# was filled" without ever storing the value.
TypedTextKind = Literal["empty", "numeric", "email_like", "url_like", "secret_like", "text"]

_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 500

# Schemes that never contribute evidence: inline payloads, script, browser-internal, and local files
# (a local path is not a web origin and can leak filesystem structure).
_BLOCKED_SCHEMES = frozenset(
    {"data", "javascript", "blob", "file", "about", "chrome", "chrome-extension",
     "edge", "brave", "vivaldi", "opera", "moz-extension", "view-source", "ws", "wss"}
)
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# A query key is dropped entirely (key and value) when it looks security-sensitive. Matched
# case-insensitively as a substring so ``access_token``, ``X-Api-Key``, ``sessionId``, ``sig`` all hit.
_SENSITIVE_KEY_RE = re.compile(
    r"(token|auth|session|sid|secret|password|passwd|pwd|api[_-]?key|apikey|access|refresh|"
    r"bearer|credential|signature|sig|code|otp|nonce|assertion|saml|id[_-]?token|key)",
    re.IGNORECASE,
)

# Value shapes that look like a secret regardless of the key name (a long high-entropy token, a JWT).
_SECRET_VALUE_RE = re.compile(r"\b(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9._-]{20,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\b")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LONG_DIGITS_RE = re.compile(r"\d{12,}")


@dataclass(frozen=True)
class SanitizedUrl:
    """The safe projection of a URL: scheme+host(+port), path, and only allowlisted query keys."""

    origin: str
    path: str
    query_keys: tuple[str, ...] = ()
    dropped_query: bool = False
    truncated: bool = False

    @property
    def safe_url(self) -> str:
        """A reconstructable URL carrying no userinfo, no fragment, and no query values."""
        base = f"{self.origin}{self.path}"
        if self.query_keys:
            return base + "?" + "&".join(f"{k}=" for k in self.query_keys)
        return base


@dataclass(frozen=True)
class RedactedText:
    """Length + coarse type of a typed-text field — never the characters themselves."""

    length: int
    kind: TypedTextKind
    redacted: bool = True


@dataclass(frozen=True)
class EvidenceItem:
    """A sanitized, trust-labelled unit ready for the consolidator (design §9.3)."""

    trust: TrustClass
    kind: str
    url: SanitizedUrl | None = None
    title: str | None = None
    text: RedactedText | None = None
    fields: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- excluded origins


def _split_origin(value: str) -> tuple[str | None, str | None]:
    """``(scheme_or_None, host_or_None)`` for an origin/host pattern, lowercased, port-stripped."""
    v = (value or "").strip().lower().rstrip("/")
    if not v:
        return None, None
    if "://" in v:
        parts = urlsplit(v)
        return parts.scheme or None, (parts.hostname or None)
    # bare host or *.host
    host = v.split("/", 1)[0].split(":", 1)[0]
    return None, (host or None)


def origin_matches(url_or_origin: str, pattern: str) -> bool:
    """Whether a URL/origin matches an excluded-origin ``pattern`` (design §13.4).

    ``pattern`` may be an exact origin (``https://bank.example``), a bare host (``bank.example``), or a
    host wildcard (``*.health.example`` / ``https://*.health.example``). A wildcard matches the apex
    and any subdomain; an apex pattern never matches a look-alike suffix (``notbank.example``).
    """
    p_scheme, p_host = _split_origin(pattern)
    if not p_host:
        return False
    u_scheme, u_host = _split_origin(url_or_origin)
    if not u_host:
        return False
    if p_scheme and u_scheme and p_scheme != u_scheme:
        return False
    if p_host.startswith("*."):
        suffix = p_host[2:]
        return u_host == suffix or u_host.endswith("." + suffix)
    return u_host == p_host


def is_excluded_origin(url_or_origin: str, patterns: list[str] | tuple[str, ...]) -> bool:
    """True if the URL/origin matches any excluded pattern."""
    return any(origin_matches(url_or_origin, p) for p in (patterns or ()))


def get_excluded_origins() -> list[str]:
    """Excluded origins from ``TABVIS_BROWSER_MEMORY_EXCLUDE_ORIGINS`` (comma-separated), else []."""
    raw = os.environ.get("TABVIS_BROWSER_MEMORY_EXCLUDE_ORIGINS")
    if not raw:
        return []
    return [p for p in (s.strip() for s in raw.split(",")) if p]


# --------------------------------------------------------------------------- URL sanitization


def sanitize_url(
    url: str,
    *,
    allow_query_keys: frozenset[str] | set[str] | None = None,
    excluded_origins: list[str] | tuple[str, ...] | None = None,
) -> SanitizedUrl | None:
    """Reduce a URL to its safe projection, or ``None`` if it must be dropped entirely.

    Dropped when: the scheme is unsupported/blocked, there is no host, or the origin is excluded.
    Otherwise userinfo and fragment are stripped, every query value is removed, and a query key is
    kept only if it is in ``allow_query_keys`` AND does not itself look sensitive.
    """
    if not url or not isinstance(url, str):
        return None
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None

    scheme = (parts.scheme or "").lower()
    if scheme in _BLOCKED_SCHEMES or scheme not in _ALLOWED_SCHEMES:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if excluded_origins and is_excluded_origin(f"{scheme}://{host}", excluded_origins):
        return None

    if ":" in host:  # IPv6 literal — urlsplit strips the [] the URL form requires
        host = f"[{host}]"
    netloc = host + (f":{parts.port}" if parts.port else "")  # userinfo dropped by not re-adding it
    origin = f"{scheme}://{netloc}"

    path, truncated = _truncate(parts.path or "", _MAX_SNIPPET_CHARS)

    allow = {k.lower() for k in (allow_query_keys or set())}
    kept: list[str] = []
    dropped = False
    for key, _value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k in allow and not _SENSITIVE_KEY_RE.search(k):
            kept.append(key)
        else:
            dropped = True
    # de-duplicate while preserving order
    seen: set[str] = set()
    kept_unique = tuple(k for k in kept if not (k in seen or seen.add(k)))

    return SanitizedUrl(
        origin=origin,
        path=path,
        query_keys=kept_unique,
        dropped_query=dropped,
        truncated=truncated,
    )


# --------------------------------------------------------------------------- text / typed input


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def sanitize_title(title: str | None) -> str | None:
    """Normalize + truncate a page title. Titles are display strings, not instructions."""
    if not title or not isinstance(title, str):
        return None
    norm = unicodedata.normalize("NFC", title).strip()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return None
    out, _ = _truncate(norm, _MAX_TITLE_CHARS)
    return out


def classify_typed_text(text: str) -> TypedTextKind:
    """Coarse classification used only to label a redacted field (never stored with the value)."""
    if not text:
        return "empty"
    if _JWT_RE.search(text) or _SECRET_VALUE_RE.search(text) or _LONG_DIGITS_RE.search(text):
        return "secret_like"
    if _EMAIL_RE.match(text):
        return "email_like"
    if text.strip().lower().startswith(("http://", "https://")):
        return "url_like"
    if text.isdigit():
        return "numeric"
    return "text"


def redact_typed_text(text: str | None) -> RedactedText:
    """Replace typed text with length + coarse type. The characters never leave this function."""
    if text is None or not isinstance(text, str):
        return RedactedText(length=0, kind="empty")
    return RedactedText(length=len(text), kind=classify_typed_text(text))


# --------------------------------------------------------------------------- filesystem paths


def safe_evidence_path(path: str, root: str) -> str | None:
    """Return the realpath of ``path`` if it stays inside ``root``, else ``None`` (design §9.2.10).

    Guards evidence readers against a hostile artifact/download reference (``../../etc/passwd``,
    absolute paths, symlink escapes) redirecting a read outside the resolved session/artifact root.
    """
    if not path or not root:
        return None
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, path))
    if target == root_real or target.startswith(root_real + os.sep):
        return target
    return None
