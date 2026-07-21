"""Redaction for the explain view (design §11.7).

``explain`` must reveal *what* shaped the context — provider, source ref, size, include/drop reason —
without leaking the content itself for anything sensitive. Public and workspace content is shown;
sensitive and secret-ref content is replaced with a marker while all provenance is preserved.
"""

from __future__ import annotations

from tabvis.gateway.runtime.context.pack import PUBLIC, SECRET_REF, SENSITIVE, WORKSPACE

_VISIBLE = {PUBLIC, WORKSPACE}


def redact_for_display(sensitivity: str, content: str) -> str:
    if sensitivity in _VISIBLE:
        return content
    return f"[redacted:{sensitivity}]"
