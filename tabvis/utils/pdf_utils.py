"""PDF document helpers

Page-range parsing for the FileRead tool's PDF path, model-capability detection (Haiku 3 is
the only remaining model that predates native PDF document blocks → falls back to the
poppler-utils page-extraction path), and the document-extension check.

Casing: Python identifiers are snake_case. ``parse_pdf_page_range`` returns a plain ``dict``
with **snake_case** keys (``first_page``/``last_page``); this is an internal value object, not a
JSON/API/transcript wire payload, so there are no camelCase wire keys to preserve. The TS
``{ firstPage, lastPage }`` object is consumed only in-process.

Faithful-behavior notes:
- TS ``parseInt(str, 10)`` parses a leading integer prefix and ignores trailing junk
  (``parseInt('5abc', 10) === 5``) and surrounding sign/whitespace is already trimmed off the
  segment by the caller's slicing. ``_parse_int10`` reproduces that leading-digits semantics
  (returns ``None`` for "NaN", matching ``isNaN`` rejection in TS).
- Open-ended ranges (``"3-"``) map ``last_page`` to ``math.inf`` (TS ``Infinity``).
- 1-indexed pages; zero / negative / inverted ranges return ``None``.
"""

from __future__ import annotations

import math
import re

from tabvis.utils.model.model import get_main_loop_model

# Document extensions that are handled specially.
DOCUMENT_EXTENSIONS = frozenset({"pdf"})

__all__ = [
    "DOCUMENT_EXTENSIONS",
    "is_pdf_extension",
    "is_pdf_supported",
    "parse_pdf_page_range",
]

# Leading-integer prefix (optional sign), matching JS parseInt(str, 10) for base-10 input.
_LEADING_INT_RE = re.compile(r"^[+-]?\d+")


def _parse_int10(text: str) -> int | None:
    """``None`` when the result would be ``NaN``.

    JS ``parseInt`` consumes an optional sign + leading run of decimal digits and ignores the
    trailing remainder, returning ``NaN`` only when no leading digits are present.
    """
    match = _LEADING_INT_RE.match(text)
    if match is None:
        return None
    return int(match.group(0))


def parse_pdf_page_range(pages: str) -> dict[str, int | float] | None:
    """Parse a page-range string into ``{first_page, last_page}`` (1-indexed) or ``None``.

    Supported formats:
    - ``"5"``    → ``{first_page: 5, last_page: 5}``
    - ``"1-10"`` → ``{first_page: 1, last_page: 10}``
    - ``"3-"``   → ``{first_page: 3, last_page: math.inf}`` (open-ended)

    Finite pages are plain ``int`` (1-indexed); the open-ended ``last_page`` is :data:`math.inf`
    (TS ``Infinity``) so callers can ``== math.inf`` it exactly as the TS ``=== Infinity`` check.
    Returns ``None`` on invalid input (non-numeric, zero, inverted range).
    """
    trimmed = pages.strip()
    if not trimmed:
        return None

    # "N-" open-ended range.
    if trimmed.endswith("-"):
        first = _parse_int10(trimmed[:-1])
        if first is None or first < 1:
            return None
        return {"first_page": first, "last_page": math.inf}

    dash_index = trimmed.find("-")
    if dash_index == -1:
        # Single page: "5".
        page = _parse_int10(trimmed)
        if page is None or page < 1:
            return None
        return {"first_page": page, "last_page": page}

    # Range: "1-10".
    first = _parse_int10(trimmed[:dash_index])
    last = _parse_int10(trimmed[dash_index + 1 :])
    if (
        first is None
        or last is None
        or first < 1
        or last < 1
        or last < first
    ):
        return None
    return {"first_page": first, "last_page": last}


def is_pdf_supported() -> bool:
    """Whether PDF document blocks work with the current main-loop model.

    PDF document blocks work on supported model API providers. Haiku 3 is the only remaining
    model that predates PDF support; users on it fall back to the page-extraction path.
    """
    return "claude-3-haiku" not in get_main_loop_model().lower()


def is_pdf_extension(ext: str) -> bool:
    """Whether ``ext`` (with or without a leading dot) names a PDF document."""
    normalized = ext[1:] if ext.startswith(".") else ext
    return normalized.lower() in DOCUMENT_EXTENSIONS
