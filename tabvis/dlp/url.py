"""URL scrubbing for DLP egress (design §11.2).

Removes the parts of a URL that can carry a secret before it leaves the trusted domain: userinfo
(``user:pass@``), the fragment, and **all query values** (keys are kept as ``key=`` so structure is
still legible, values are dropped). Path is preserved. Anything unparseable returns a safe redaction
rather than the original string.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def clean_url(url: str) -> str:
    """Strip userinfo, fragment and all query values from a URL (design §11.2)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[redacted-url]"
    # rebuild netloc without userinfo
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    # keep query KEYS, drop every value
    if parts.query:
        pairs = [(k, "") for k, _v in parse_qsl(parts.query, keep_blank_values=True)]
        query = urlencode(pairs)
    else:
        query = ""
    return urlunsplit((parts.scheme, host, parts.path, query, ""))  # fragment dropped
