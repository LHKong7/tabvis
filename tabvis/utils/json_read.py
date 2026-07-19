"""Leaf ``strip_bom`` helper

Extracted from ``json.ts`` in the source to break an import cycle (settings → json →
log → types/logs → … → settings). ``json.ts`` imports this for its memoized,
logging ``safeParseJSON``; leaf callers that cannot import ``json.ts`` use
``stripBOM`` + ``JSON.parse`` inline.

Why it exists: PowerShell 5.x writes UTF-8 *with* a BOM by default (``Out-File``,
``Set-Content``). We can't control user environments, so the BOM is stripped on
read — without this, ``JSON.parse`` (Python: ``json.loads``) fails on the leading
``U+FEFF``.

Casing: Python identifier snake_case (``strip_bom``); ``UTF8_BOM`` is an
UPPER_CASE const.
"""

from __future__ import annotations

# UTF-8 BOM (the U+FEFF code point as decoded text — a single character, not the
# 3-byte 0xEF 0xBB 0xBF sequence; this operates on already-decoded ``str``).
UTF8_BOM = "﻿"


def strip_bom(content: str) -> str:
    """Strip a leading UTF-8 BOM (``U+FEFF``) from ``content`` if present."""
    return content[1:] if content.startswith(UTF8_BOM) else content
