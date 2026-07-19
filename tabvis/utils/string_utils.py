"""General string utilities + safe string accumulation

The TS module is a grab-bag of pure string helpers (regex escaping, capitalization,
pluralization, first-line/char-count, full-width digit/space normalization), a
size-bounded ``safeJoinLines`` joiner, the ``EndTruncatingAccumulator`` class, and a
line-count truncator. All are zero-dependency leaf logic.

Casing: Python identifiers are snake_case (``escape_reg_exp``, ``safe_join_lines``); the
class is :class:`EndTruncatingAccumulator` (PascalCase). There are no wire-key dicts here,
so nothing round-trips to JSON/the API — only identifier names change.

Faithful-behavior notes:

* ``escape_reg_exp`` reproduces the JS ``/[.*+?^${}()|[\\]\\\\]/g`` escape set verbatim. The
  replacement ``\\$&`` (backref to the whole match) becomes ``\\\\\\g<0>`` in Python's
  ``re.sub``.
* ``capitalize`` upcases only the first char (NOT lodash-style — the rest is untouched).
* ``count_char_in_string`` keeps the ``indexOf``-jump algorithm (``str.find``); it operates
  on any object exposing ``find`` (``str``/``bytes`` both work, like the TS Buffer note).
* ``MAX_STRING_LENGTH`` is ``2**25`` (32 MiB of chars), matching the TS constant.
* :class:`EndTruncatingAccumulator` measures sizes in *characters* (``len``), exactly like the
  TS ``.length`` on a JS string; ``append`` accepts ``str`` or ``bytes`` (the TS ``Buffer``
  branch decodes via ``.toString()`` — here ``bytes.decode()``).
"""

from __future__ import annotations

import math
import re

# Escapes special regex characters so a string can be used as a literal RegExp pattern.
# Mirrors the JS character class /[.*+?^${}()|[\]\\]/g exactly.
_ESCAPE_REG_EXP_PATTERN = re.compile(r"[.*+?^${}()|[\]\\]")


def escape_reg_exp(value: str) -> str:
    """Escape special regex characters so ``value`` is a literal pattern.

    The TS replacement ``'\\$&'`` (escape the whole match) becomes
    ``\\\\\\g<0>`` here so each matched char is prefixed with a backslash.
    """
    return _ESCAPE_REG_EXP_PATTERN.sub(r"\\\g<0>", value)


def capitalize(value: str) -> str:
    """Uppercase the first character, leaving the rest unchanged.

    Unlike ``str.capitalize()``/lodash, this does NOT lowercase the
    remaining characters: ``capitalize('fooBar') -> 'FooBar'``.
    """
    return value[:1].upper() + value[1:]


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    """Return the singular or plural form of ``word`` based on ``n``.

    ``Plural_word`` defaults to ``word + 's'`` (computed lazily so the
    default tracks the supplied ``word``, matching the TS ``pluralWord = word + 's'`` default).
    """
    if plural_word is None:
        plural_word = word + "s"
    return word if n == 1 else plural_word


def first_line_of(s: str) -> str:
    """Return the first line of ``s`` without allocating a split array.

    If there is no newline, the whole string is returned.
    """
    nl = s.find("\n")
    return s if nl == -1 else s[:nl]


def count_char_in_string(s: str | bytes, char: str | bytes, start: int = 0) -> int:
    """Count occurrences of ``char`` in ``s`` using ``find`` jumps.

    Structurally typed on ``find`` so ``str``/``bytes`` both
    work (the TS note about ``Buffer.indexOf`` accepting string needles).
    """
    count = 0
    i = s.find(char, start)  # type: ignore[arg-type]
    while i != -1:
        count += 1
        i = s.find(char, i + 1)  # type: ignore[arg-type]
    return count


# Full-width (zenkaku) digits U+FF10..U+FF19 -> ASCII via the 0xFEE0 offset.
_FULL_WIDTH_DIGIT_PATTERN = re.compile("[０-９]")


def normalize_full_width_digits(value: str) -> str:
    """Normalize full-width (zenkaku) digits to half-width ASCII digits.

    Each matched char is shifted down by ``0xFEE0``
    (``String.fromCharCode(ch.charCodeAt(0) - 0xfee0)``).
    """
    return _FULL_WIDTH_DIGIT_PATTERN.sub(lambda m: chr(ord(m.group(0)) - 0xFEE0), value)


def normalize_full_width_space(value: str) -> str:
    """Normalize the full-width (zenkaku) space U+3000 to a half-width space.

    Normalize the full width space.
    """
    return value.replace("　", " ")


# Keep in-memory accumulation modest to avoid excessive memory use. Callers decide how to handle
# content beyond this limit.
MAX_STRING_LENGTH = 2**25


def safe_join_lines(
    lines: list[str],
    delimiter: str = ",",
    max_size: int = MAX_STRING_LENGTH,
) -> str:
    """Join ``lines`` with ``delimiter``, truncating if the result exceeds ``max_size``.

    Faithful to the early-return on the first line that does not
    fit: if there is leftover room a partial line + ``'...[truncated]'`` is appended, else just
    the marker — then the accumulated string is returned immediately.
    """
    truncation_marker = "...[truncated]"
    result = ""

    for line in lines:
        delimiter_to_add = delimiter if result else ""
        full_addition = delimiter_to_add + line

        if len(result) + len(full_addition) <= max_size:
            # The full line fits.
            result += full_addition
        else:
            # Need to truncate.
            remaining_space = (
                max_size - len(result) - len(delimiter_to_add) - len(truncation_marker)
            )

            if remaining_space > 0:
                # Add delimiter and as much of the line as will fit.
                result += delimiter_to_add + line[:remaining_space] + truncation_marker
            else:
                # No room for any of this line, just add truncation marker.
                result += truncation_marker
            return result
    return result


class EndTruncatingAccumulator:
    """Size-bounded string accumulator that truncates from the end.

    Preserves the *beginning* of the output
    (prevents ``RangeError``-style blowups) and reports how much was dropped via ``__str__``.

    Sizes are measured in characters (``len``), matching the JS string ``.length``.
    """

    def __init__(self, max_size: int = MAX_STRING_LENGTH) -> None:
        self._max_size = max_size
        self._content: str = ""
        self._is_truncated = False
        self._total_bytes_received = 0

    def append(self, data: str | bytes) -> None:
        """Append ``data``; truncate the tail once total size exceeds ``max_size``.

        ``bytes`` input is decoded (the TS ``Buffer`` branch's ``.toString()``).
        """
        s = data if isinstance(data, str) else data.decode()
        self._total_bytes_received += len(s)

        # If already at capacity and truncated, don't modify content.
        if self._is_truncated and len(self._content) >= self._max_size:
            return

        # Check if adding the string would exceed the limit.
        if len(self._content) + len(s) > self._max_size:
            # Only append what we can fit.
            remaining_space = self._max_size - len(self._content)
            if remaining_space > 0:
                self._content += s[:remaining_space]
            self._is_truncated = True
        else:
            self._content += s

    def __str__(self) -> str:
        """Return the accumulated string, with a truncation marker if truncated."""
        if not self._is_truncated:
            return self._content

        truncated_bytes = self._total_bytes_received - self._max_size
        # JS Math.round is round-half-UP, not Python round()'s banker's rounding — match it so the
        # "KB removed" figure is byte-identical to the TS on exact .5 boundaries.
        truncated_kb = math.floor(truncated_bytes / 1024 + 0.5)
        return self._content + f"\n... [output truncated - {truncated_kb}KB removed]"

    def clear(self) -> None:
        """Clear all accumulated data."""
        self._content = ""
        self._is_truncated = False
        self._total_bytes_received = 0

    @property
    def length(self) -> int:
        """Current size of accumulated (retained) data."""
        return len(self._content)

    @property
    def truncated(self) -> bool:
        """Whether truncation has occurred."""
        return self._is_truncated

    @property
    def total_bytes(self) -> int:
        """Total bytes received (before truncation)."""
        return self._total_bytes_received


def truncate_to_lines(text: str, max_lines: int) -> str:
    """Truncate ``text`` to ``max_lines`` lines, adding an ellipsis if truncated.

    The ellipsis is the single char ``'…'`` (U+2026), matching TS.
    """
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + "…"
