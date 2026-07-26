"""Text & header scrubbing for DLP egress (design §11.2).

Pure, deterministic redactions applied to outbound text and structured fields:

* drop the credential-bearing headers entirely (``Cookie`` / ``Set-Cookie`` / ``Authorization`` /
  ``Proxy-Authorization``);
* drop storage-state / local-storage / session-storage values;
* coarsely mask identifiers (email / phone) per policy;
* reduce an exception to its type + a stable code, never its args.

The canary value-scan is layered on top by :mod:`tabvis.dlp.gateway`; this module is the format-based
redaction that runs regardless of whether a value is a known canary (unknown-format secrets can't be
recognized — §11.4).
"""

from __future__ import annotations

import re

_SENSITIVE_HEADERS = {
    "cookie",
    "set-cookie",
    "authorization",
    "proxy-authorization",
    "x-api-key",
}
_SENSITIVE_KEYS = re.compile(
    r"(cookie|authorization|token|secret|password|passwd|pwd|storage_state|local_?storage|"
    r"session_?storage|api[_-]?key|credential)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Do not start a phone match in a URL/path/query token.  The old digit-only boundary treated
# opaque web identifiers as phone numbers, so paths such as ``/document-1991237455038119936`` and
# ``/Archives/edgar/data/1577551/...`` were rewritten to ``[redacted]`` before the browser result
# reached the model.  A later digit cannot become a partial match because the preceding character
# is itself a digit.  Natural-language phone numbers (including the formatted form covered by the
# policy) still match.
_PHONE = re.compile(
    r"(?<![\w/=?#&.\-])"
    # ISO calendar dates are public facts, not identifiers. Keep this guard adjacent to the phone
    # pattern so a date at the start of a sentence is not redacted merely because it has ten digits.
    r"(?!\d{4}-\d{2}-\d{2}(?!\d))"
    r"(\+?\d[\d\-\s]{7,}\d)"
    r"(?![\w/?#&.\-])"
)

REDACTED = "[redacted]"


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy with credential-bearing header values removed (design §11.2)."""
    out: dict[str, str] = {}
    for key, value in headers.items():
        out[key] = REDACTED if key.lower() in _SENSITIVE_HEADERS else value
    return out


def mask_identifiers(text: str) -> str:
    """Mask emails and phone numbers in free text (design §11.2 identifier de-identification)."""
    if not text:
        return text
    text = _EMAIL.sub(REDACTED, text)
    text = _PHONE.sub(REDACTED, text)
    return text


def redact_mapping(data: dict) -> dict:
    """Recursively redact values whose key looks sensitive (form field values, storage, tokens)."""
    out: dict = {}
    for key, value in data.items():
        if isinstance(key, str) and _SENSITIVE_KEYS.search(key):
            out[key] = REDACTED
        elif isinstance(value, dict):
            out[key] = redact_mapping(value)
        elif isinstance(value, list):
            out[key] = [redact_mapping(v) if isinstance(v, dict) else v for v in value]
        else:
            out[key] = value
    return out


def redact_exception(exc: BaseException) -> str:
    """Reduce an exception to its type name only — never its args (design §11.2, §12.1)."""
    return f"{type(exc).__name__}"
