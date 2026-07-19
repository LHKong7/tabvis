"""Width-aware truncation/wrapping

The TS module is *not* leaf-safe: it measures display width with ``stringWidth`` (from
``src/utils/terminal/stringWidth.ts``) and splits on grapheme boundaries via the
``Intl.Segmenter`` accessor in ``src/utils/intl.ts``. Both behaviours are existing
in the tabvis tree:

* ``stringWidth`` → :func:`tabvis.utils.slice_ansi.string_width` (the same faithful slice of
  ``terminal/stringWidth.ts`` — ``ambiguousAsWide: false``, grapheme-cluster width = the
  first non-zero-width char's wcwidth). That module is the single implemented home for the width
  function, so this file reuses it rather than re-implementing it.
* ``getGraphemeSegmenter()`` → :func:`tabvis.utils.intl.get_grapheme_segmenter` (returns
  :func:`tabvis.utils.intl.segment_graphemes`, a stdlib grapheme clusterer).

The ellipsis is the single-char ``…`` (U+2026 HORIZONTAL ELLIPSIS), width 1, exactly as in
the TS source.

Casing: Python identifiers snake_case; this module returns/accepts plain ``str`` (no wire-key
dicts). Defaults match the TS signatures (``single_line=False``).
"""

from __future__ import annotations

from tabvis.utils.intl import get_grapheme_segmenter
from tabvis.utils.slice_ansi import string_width

# U+2026 HORIZONTAL ELLIPSIS — the single visible cell the TS appends/prepends.
ELLIPSIS = "…"

__all__ = [
    "ELLIPSIS",
    "truncate",
    "truncate_path_middle",
    "truncate_start_to_width",
    "truncate_to_width",
    "truncate_to_width_no_ellipsis",
    "wrap_text",
]


def truncate_path_middle(path: str, max_length: int) -> str:
    """Truncate a file path in the middle to preserve both directory context and filename.

    Width-aware: uses :func:`string_width` for correct CJK/emoji measurement. For example
    ``"src/features/deeply/nested/folder/MyModule.ts"`` becomes ``"src/features/…/MyModule.ts"``
    when ``max_length`` is 30.

    Args:
        path: The file path to truncate.
        max_length: Maximum display width of the result in terminal columns (must be > 0).

    Returns:
        The truncated path, or the original if it fits within ``max_length``.
    """
    # No truncation needed.
    if string_width(path) <= max_length:
        return path

    # Handle edge case of very small or non-positive max_length.
    if max_length <= 0:
        return "…"

    # Need at least room for "…" + something meaningful.
    if max_length < 5:
        return truncate_to_width(path, max_length)

    # Find the filename (last path segment).
    last_slash = path.rfind("/")
    # Include the leading slash in filename for display.
    filename = path[last_slash:] if last_slash >= 0 else path
    directory = path[:last_slash] if last_slash >= 0 else ""
    filename_width = string_width(filename)

    # If filename alone is too long, truncate from start.
    if filename_width >= max_length - 1:
        return truncate_start_to_width(path, max_length)

    # Calculate space available for directory prefix.
    # Result format: directory + "…" + filename.
    available_for_dir = max_length - 1 - filename_width  # -1 for ellipsis

    if available_for_dir <= 0:
        # No room for directory, just show filename (truncated if needed).
        return truncate_start_to_width(filename, max_length)

    # Truncate directory and combine.
    truncated_dir = truncate_to_width_no_ellipsis(directory, available_for_dir)
    return truncated_dir + "…" + filename


def truncate_to_width(text: str, max_width: int) -> str:
    """Truncate ``text`` to fit within ``max_width`` display columns, appending ``…``.

    Splits on grapheme boundaries to avoid breaking emoji or surrogate pairs. The ellipsis is
    only appended when truncation actually occurs.
    """
    if string_width(text) <= max_width:
        return text
    if max_width <= 1:
        return "…"
    width = 0
    result = ""
    for segment in get_grapheme_segmenter()(text):
        seg_width = string_width(segment)
        if width + seg_width > max_width - 1:
            break
        result += segment
        width += seg_width
    return result + "…"


def truncate_start_to_width(text: str, max_width: int) -> str:
    """Truncate from the *start* of ``text``, keeping the tail end and prepending ``…``.

    Width-aware and grapheme-safe.
    """
    if string_width(text) <= max_width:
        return text
    if max_width <= 1:
        return "…"
    segments = list(get_grapheme_segmenter()(text))
    width = 0
    start_idx = len(segments)
    for i in range(len(segments) - 1, -1, -1):
        seg_width = string_width(segments[i])
        if width + seg_width > max_width - 1:  # -1 for '…'
            break
        width += seg_width
        start_idx = i
    return "…" + "".join(segments[start_idx:])


def truncate_to_width_no_ellipsis(text: str, max_width: int) -> str:
    """Truncate ``text`` to fit within ``max_width`` display columns, *without* an ellipsis.

    Useful when the caller adds its own separator (e.g. middle-truncation with ``…`` between
    parts). Width-aware and grapheme-safe.
    """
    if string_width(text) <= max_width:
        return text
    if max_width <= 0:
        return ""
    width = 0
    result = ""
    for segment in get_grapheme_segmenter()(text):
        seg_width = string_width(segment)
        if width + seg_width > max_width:
            break
        result += segment
        width += seg_width
    return result


def truncate(str_: str, max_width: int, single_line: bool = False) -> str:
    """Truncate ``str_`` to ``max_width`` display columns, splitting on grapheme boundaries.

    Avoids breaking emoji, CJK, or surrogate pairs. Appends ``…`` when truncation occurs.

    Args:
        str_: The string to truncate.
        max_width: Maximum display width in terminal columns.
        single_line: If ``True``, also truncates at the first newline.

    Returns:
        The truncated string with an ellipsis if needed.
    """
    result = str_

    # If single_line is true, truncate at first newline.
    if single_line:
        first_newline = str_.find("\n")
        if first_newline != -1:
            result = str_[:first_newline]
            # Ensure total width including ellipsis doesn't exceed max_width.
            if string_width(result) + 1 > max_width:
                return truncate_to_width(result, max_width)
            return f"{result}…"

    if string_width(result) <= max_width:
        return result
    return truncate_to_width(result, max_width)


def wrap_text(text: str, width: int) -> list[str]:
    """Greedy grapheme-by-grapheme wrap of ``text`` into lines of at most ``width`` columns.

    A grapheme that would overflow the current line starts a new line
    (no whitespace-aware word wrapping — matches the TS exactly).
    """
    lines: list[str] = []
    current_line = ""
    current_width = 0

    for segment in get_grapheme_segmenter()(text):
        seg_width = string_width(segment)
        if current_width + seg_width <= width:
            current_line += segment
            current_width += seg_width
        else:
            if current_line:
                lines.append(current_line)
            current_line = segment
            current_width = seg_width

    if current_line:
        lines.append(current_line)
    return lines
