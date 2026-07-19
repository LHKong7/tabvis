"""ANSI-aware text segmentation by highlight ranges

``segment_text_by_highlights(text, highlights)`` splits a (possibly ANSI-styled) string into
:class:`TextSegment` pieces, attaching a :class:`TextHighlight` to each highlighted span. Highlight
ranges are measured in **visible** positions (excluding ANSI escape codes); the segmenter tracks
two position systems — "visible" (what the user sees) and "string" (raw, including ANSI codes) —
so each emitted segment carries the correct prefix/suffix style codes to render in isolation.

The TS leans on four functions from ``@alcalzone/ansi-tokenize`` (``tokenize``, ``reduceAnsiCodes``,
``ansiCodesToString``, ``undoAnsiCodes``) plus the ``AnsiCode``/``Token`` types. None of those are a
published Python package, but Tabvis already implements the required subset in
``tabvis/utils/slice_ansi.py`` (byte-checked against the upstream sources). This module reuses those
implementation rather than duplicating the tokenizer.

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; :class:`TextHighlight`
and :class:`TextSegment` are dataclasses (PascalCase). ``color`` here is a theme key string
(``keyof Theme``) kept verbatim — it is the same theme-key surface preserved verbatim in
``tabvis/utils/theme.py`` — so no casing is rewritten on the highlight payload fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tabvis.utils.slice_ansi import (
    AnsiCode,
    _ansi_codes_to_string,
    _reduce_ansi_codes,
    _undo_ansi_codes,
    tokenize,
)
from tabvis.utils.theme import Theme  # noqa: F401 — documents the keyof-Theme color surface


@dataclass
class TextHighlight:
    """A highlight span over visible positions ``[start, end)``.

    ``color`` / ``shimmer_color`` are ``keyof Theme`` strings (verbatim theme keys) or ``None``.
    ``priority`` breaks ties when two highlights start at the same position (higher wins).
    """

    start: int
    end: int
    color: str | None
    priority: int
    dim_color: bool | None = None
    inverse: bool | None = None
    shimmer_color: str | None = None


@dataclass
class TextSegment:
    """A piece of the original text, optionally carrying a resolved :class:`TextHighlight`."""

    text: str
    start: int
    highlight: TextHighlight | None = None


def segment_text_by_highlights(
    text: str, highlights: list[TextHighlight]
) -> list[TextSegment]:
    """Split ``text`` into segments, attaching non-overlapping highlights.

    Highlights are sorted by ``start`` (ascending), ties broken by ``priority`` (descending).
    Zero-length highlights are skipped, and any highlight that overlaps an already-claimed range
    is dropped — earlier (higher-priority) highlights win.
    """
    if len(highlights) == 0:
        return [TextSegment(text=text, start=0)]

    sorted_highlights = sorted(
        highlights, key=lambda h: (h.start, -h.priority)
    )

    resolved_highlights: list[TextHighlight] = []
    used_ranges: list[tuple[int, int]] = []

    for highlight in sorted_highlights:
        if highlight.start == highlight.end:
            continue

        overlaps = any(
            (range_start <= highlight.start < range_end)
            or (range_start < highlight.end <= range_end)
            or (highlight.start <= range_start and highlight.end >= range_end)
            for range_start, range_end in used_ranges
        )

        if not overlaps:
            resolved_highlights.append(highlight)
            used_ranges.append((highlight.start, highlight.end))

    return _HighlightSegmenter(text).segment(resolved_highlights)


@dataclass
class _HighlightSegmenter:
    """Stateful cursor over the tokenized text.

    Two position systems: "visible" (what the user sees, excluding ANSI codes) and "string" (raw
    positions including ANSI codes for substring extraction).
    """

    text: str
    _visible_pos: int = 0
    _string_pos: int = 0
    _token_idx: int = 0
    _char_idx: int = 0  # offset within the current text token (for partial consumption)
    _codes: list[AnsiCode] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._tokens = tokenize(self.text)

    def segment(self, highlights: list[TextHighlight]) -> list[TextSegment]:
        segments: list[TextSegment] = []

        for highlight in highlights:
            before = self._segment_to(highlight.start)
            if before is not None:
                segments.append(before)

            highlighted = self._segment_to(highlight.end)
            if highlighted is not None:
                highlighted.highlight = highlight
                segments.append(highlighted)

        after = self._segment_to(float("inf"))
        if after is not None:
            segments.append(after)

        return segments

    def _segment_to(self, target_visible_pos: float) -> TextSegment | None:
        if self._token_idx >= len(self._tokens) or target_visible_pos <= self._visible_pos:
            return None

        visible_start = self._visible_pos

        # Consume leading ANSI codes before the first visible char.
        while self._token_idx < len(self._tokens):
            token = self._tokens[self._token_idx]
            if token.type != "ansi":
                break
            self._codes.append(AnsiCode(code=token.code or "", end_code=token.end_code or ""))
            self._string_pos += len(token.code or "")
            self._token_idx += 1

        string_start = self._string_pos
        codes_start = list(self._codes)

        # Advance through tokens until we reach the target.
        while self._visible_pos < target_visible_pos and self._token_idx < len(self._tokens):
            token = self._tokens[self._token_idx]

            if token.type == "ansi":
                self._codes.append(
                    AnsiCode(code=token.code or "", end_code=token.end_code or "")
                )
                self._string_pos += len(token.code or "")
                self._token_idx += 1
            else:
                token_value = token.value or ""
                chars_needed = target_visible_pos - self._visible_pos
                chars_available = len(token_value) - self._char_idx
                chars_to_take = int(min(chars_needed, chars_available))

                self._string_pos += chars_to_take
                self._visible_pos += chars_to_take
                self._char_idx += chars_to_take

                if self._char_idx >= len(token_value):
                    self._token_idx += 1
                    self._char_idx = 0

        # Empty segment (can occur when only trailing ANSI codes remain).
        if self._string_pos == string_start:
            return None

        prefix_codes = _reduce_codes(codes_start)
        suffix_codes = _reduce_codes(self._codes)
        self._codes = suffix_codes

        prefix = _ansi_codes_to_string(prefix_codes)
        suffix = _ansi_codes_to_string(_undo_ansi_codes(suffix_codes))

        return TextSegment(
            text=prefix + self.text[string_start : self._string_pos] + suffix,
            start=visible_start,
        )


def _reduce_codes(codes: list[AnsiCode]) -> list[AnsiCode]:
    return [c for c in _reduce_ansi_codes(codes) if c.code != c.end_code]
