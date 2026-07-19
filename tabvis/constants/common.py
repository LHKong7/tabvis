"""Common date helpers.

``get_session_start_date`` is memoized (cached once at session start) for prompt-cache
stability.
"""

from __future__ import annotations

import functools
import os
from datetime import date


def get_local_iso_date() -> str:
    """Local date as ``YYYY-MM-DD`` (or the TABVIS_OVERRIDE_DATE verbatim)."""
    override = os.environ.get("TABVIS_OVERRIDE_DATE")
    if override:
        return override
    return date.today().strftime("%Y-%m-%d")


@functools.cache
def get_session_start_date() -> str:
    return get_local_iso_date()


def get_local_month_year() -> str:
    """Returns ``"Month YYYY"`` (e.g. ``"February 2026"``)."""
    override = os.environ.get("TABVIS_OVERRIDE_DATE")
    d = date.fromisoformat(override) if override else date.today()
    return d.strftime("%B %Y")
