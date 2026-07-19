"""Shared locale/segmentation helpers

The TS module caches expensive ``Intl`` constructors (Segmenter, RelativeTimeFormat,
DateTimeFormat) behind lazy accessors. Python has no ICU-backed ``Intl`` in the stdlib, so the
faithful slice is reproduced with stdlib primitives:

* grapheme segmentation (``getGraphemeSegmenter``/``firstGrapheme``/``lastGrapheme``) → a
  stdlib-only Unicode grapheme-cluster splitter (:func:`segment_graphemes`). It covers the
  cases the consumers care about — base char + combining marks, ZWJ emoji sequences, regional-
  indicator flag pairs, and trailing variation selectors — so "first/last visible character"
  matches user perception. (Full UAX-29 would need a third-party ICU/``grapheme`` lib; see
  ``deps_needed`` / module note.)
* word segmentation (``getWordSegmenter``) → a stdlib regex word splitter
  (:func:`segment_words`). UAX-29 word boundaries differ in edge cases; the common
  word/non-word alternation is reproduced.
* ``getRelativeTimeFormat`` → :func:`get_relative_time_format`, a tiny English formatter
  (``Intl.RelativeTimeFormat('en', ...)``; the TS always passes ``'en'``). ``narrow`` and
  ``short`` share abbreviated units; ``long`` spells them out. ``numeric='auto'`` swaps the
  ±1/0 cases to ``yesterday``/``today``/``tomorrow`` etc.
* ``getTimeZone`` → :func:`get_time_zone`, the system tz name via ``datetime``/``tzlocal``-free
  stdlib (``time.tzname`` / ``zoneinfo`` discovery), cached for the process lifetime.
* ``getSystemLocaleLanguage`` → :func:`get_system_locale_language`, the language subtag from the
  POSIX locale env, cached (``None`` sentinel for "computed but unavailable", as in TS).

Casing: Python identifiers snake_case; no wire-key dicts here. Caches use module-level state +
``functools.lru_cache`` (parity with the TS module-level ``let``/``Map`` caches).

Stdlib name note: this module is ``tabvis.utils.intl`` and is fully namespaced; nothing imports a
stdlib ``intl`` (there is none).
"""

from __future__ import annotations

import datetime as _datetime
import locale as _locale
import os
import re
import time
import unicodedata
from functools import cache
from typing import Literal

# ----------------------------------------------------------------------------------------------
# Grapheme segmentation (Intl.Segmenter granularity:'grapheme')
# ----------------------------------------------------------------------------------------------

_ZWJ = "‍"  # ZERO WIDTH JOINER
_VARIATION_SELECTORS = {chr(c) for c in range(0xFE00, 0xFE10)}  # VS1..VS16


def _is_regional_indicator(ch: str) -> bool:
    return "\U0001f1e6" <= ch <= "\U0001f1ff"


def _extends_cluster(ch: str) -> bool:
    """Whether ``ch`` attaches to the preceding base (combining mark / VS / ZWJ-adjacent)."""
    if ch in _VARIATION_SELECTORS:
        return True
    if unicodedata.combining(ch):
        return True
    # Mark categories (Mn/Mc/Me) and the joiner extend the current cluster.
    return unicodedata.category(ch).startswith("M")


def segment_graphemes(text: str) -> list[str]:
    """Split ``text`` into grapheme clusters (stdlib approximation of ``Intl.Segmenter``).

    Handles: base + combining marks/variation selectors, ZWJ-joined sequences (emoji), and
    paired regional-indicator flags. Lone code points otherwise stand alone.
    """
    clusters: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Regional-indicator pair → one flag cluster.
        if _is_regional_indicator(ch) and i + 1 < n and _is_regional_indicator(text[i + 1]):
            clusters.append(text[i : i + 2])
            i += 2
            continue
        # Start a cluster on the base char, then absorb extenders and ZWJ-joined runs.
        j = i + 1
        while j < n:
            nxt = text[j]
            if nxt == _ZWJ:
                # Consume the joiner and whatever single code point follows it.
                j += 2 if j + 1 < n else 1
                continue
            if _extends_cluster(nxt):
                j += 1
                continue
            break
        clusters.append(text[i:j])
        i = j
    return clusters


def get_grapheme_segmenter():
    """Return a callable that segments text into grapheme clusters.

    The TS returns an ``Intl.Segmenter``; here the analogue is
    :func:`segment_graphemes` itself (a stateless callable), so the cache is trivial.
    """
    return segment_graphemes


def first_grapheme(text: str) -> str:
    """Extract the first grapheme cluster from ``text`` (``''`` for empty).

        """
    if not text:
        return ""
    clusters = segment_graphemes(text)
    return clusters[0] if clusters else ""


def last_grapheme(text: str) -> str:
    """Extract the last grapheme cluster from ``text`` (``''`` for empty).

        """
    if not text:
        return ""
    clusters = segment_graphemes(text)
    return clusters[-1] if clusters else ""


# ----------------------------------------------------------------------------------------------
# Word segmentation (Intl.Segmenter granularity:'word')
# ----------------------------------------------------------------------------------------------

_WORD_SPLIT = re.compile(r"\w+|\W", re.UNICODE)


def segment_words(text: str) -> list[str]:
    """Split ``text`` into word and non-word segments (stdlib approx of UAX-29 words)."""
    return _WORD_SPLIT.findall(text)


def get_word_segmenter():
    """Return a callable that segments text into word/non-word segments.

    Return the word segmenter.
    """
    return segment_words


# ----------------------------------------------------------------------------------------------
# RelativeTimeFormat (always 'en' in the TS source)
# ----------------------------------------------------------------------------------------------

_RTF_UNITS_LONG = {
    "year": ("year", "years"),
    "quarter": ("quarter", "quarters"),
    "month": ("month", "months"),
    "week": ("week", "weeks"),
    "day": ("day", "days"),
    "hour": ("hour", "hours"),
    "minute": ("minute", "minutes"),
    "second": ("second", "seconds"),
}
_RTF_UNITS_SHORT = {
    "year": ("yr.", "yr."),
    "quarter": ("qtr.", "qtr."),
    "month": ("mo.", "mo."),
    "week": ("wk.", "wk."),
    "day": ("day", "days"),
    "hour": ("hr.", "hr."),
    "minute": ("min.", "min."),
    "second": ("sec.", "sec."),
}
# numeric:'auto' relative phrases for ±1 / 0.
_RTF_AUTO = {
    ("day", 0): "today",
    ("day", 1): "tomorrow",
    ("day", -1): "yesterday",
    ("week", 0): "this week",
    ("week", 1): "next week",
    ("week", -1): "last week",
    ("month", 0): "this month",
    ("month", 1): "next month",
    ("month", -1): "last month",
    ("year", 0): "this year",
    ("year", 1): "next year",
    ("year", -1): "last year",
    ("hour", 0): "this hour",
    ("minute", 0): "this minute",
    ("second", 0): "now",
}


class RelativeTimeFormat:
    """Minimal English ``Intl.RelativeTimeFormat`` analogue (available surface for the cache).

    ``style`` ∈ {long, short, narrow}; ``numeric`` ∈ {always, auto}. Only the ``'en'`` locale is
    supported (the TS source hardcodes ``'en'``).
    """

    def __init__(self, style: Literal["long", "short", "narrow"], numeric: Literal["always", "auto"]):
        self.style = style
        self.numeric = numeric

    def format(self, value: int, unit: str) -> str:
        if self.numeric == "auto":
            phrase = _RTF_AUTO.get((unit, value))
            if phrase is not None:
                return phrase
        table = _RTF_UNITS_LONG if self.style == "long" else _RTF_UNITS_SHORT
        singular, plural_form = table[unit]
        word = singular if abs(value) == 1 else plural_form
        if value < 0:
            return f"{abs(value)} {word} ago"
        return f"in {value} {word}"


@cache
def get_relative_time_format(
    style: Literal["long", "short", "narrow"],
    numeric: Literal["always", "auto"],
) -> RelativeTimeFormat:
    """Cached ``RelativeTimeFormat`` keyed on ``style:numeric``."""
    return RelativeTimeFormat(style, numeric)


# ----------------------------------------------------------------------------------------------
# Timezone (constant for the process lifetime)
# ----------------------------------------------------------------------------------------------

_cached_time_zone: str | None = None


def get_time_zone() -> str:
    """Return the system IANA timezone name, cached for the process lifetime.

    Prefers the
    ``TZ`` env / ``zoneinfo`` discovery; falls back to ``time.tzname``.
    """
    global _cached_time_zone
    if _cached_time_zone is None:
        tz = os.environ.get("TZ")
        if not tz:
            local_tz = _datetime.datetime.now().astimezone().tzinfo
            tz = getattr(local_tz, "key", None)  # zoneinfo exposes .key
            if not tz:
                tz = time.tzname[0] if time.tzname else "UTC"
        _cached_time_zone = tz
    return _cached_time_zone


# ----------------------------------------------------------------------------------------------
# System locale language subtag (constant for the process lifetime)
# ----------------------------------------------------------------------------------------------

# null sentinel = not yet computed; the string "" = computed-but-unavailable (TS uses
# `undefined` for that — here we use a distinct sentinel so a stripped-ICU env fails once).
_UNSET = object()
_cached_system_locale_language: object = _UNSET


def get_system_locale_language() -> str | None:
    """Return the system locale's language subtag (e.g. ``'en'``), cached.

    Reads the POSIX locale env / ``locale`` defaults; on
    failure caches and returns ``None`` (parity with the TS ``undefined`` sentinel so it fails
    once rather than retrying every call).
    """
    global _cached_system_locale_language
    if _cached_system_locale_language is _UNSET:
        lang: str | None
        try:
            raw = (
                os.environ.get("LC_ALL")
                or os.environ.get("LC_MESSAGES")
                or os.environ.get("LANG")
                or ""
            )
            if not raw or raw in ("C", "POSIX"):
                code = _locale.getdefaultlocale()[0]
                raw = code or ""
            base = raw.split(".")[0].split("@")[0]
            subtag = base.replace("_", "-").split("-")[0]
            lang = subtag.lower() if subtag else None
        except Exception:  # noqa: BLE001 - stripped-ICU/locale-less env
            lang = None
        _cached_system_locale_language = lang
    return _cached_system_locale_language  # type: ignore[return-value]
