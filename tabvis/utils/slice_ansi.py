"""Width-correct ANSI-aware string slicing

``slice_ansi(s, start, end)`` slices a string that may contain ANSI escape codes
(SGR styles **and** OSC 8 hyperlinks), measuring ``start``/``end`` in terminal
*display cells* rather than code units. Unlike the npm ``slice-ansi`` package it
handles OSC 8 hyperlink sequences correctly, because the TS uses
``@alcalzone/ansi-tokenize`` to tokenize them.

The TS file is tiny but leans on four functions from ``@alcalzone/ansi-tokenize``
(``tokenize``, ``reduceAnsiCodes``, ``ansiCodesToString``, ``undoAnsiCodes``)
plus ``stringWidth``. None of those are implemented elsewhere in the tabvis tree, so
this module implements the required subset:

* the tokenizer (SGR + OSC parsing, compound-SGR splitting, grapheme width);
* ``ansi-styles``' start->end code map (``getEndCode``);
* ``reduce_ansi_codes`` (minimize active style set) and ``undo_ansi_codes``;
* a ``string_width`` slice (stdlib ``wcwidth``, ``ambiguousAsWide: false``).

All of this is faithful to the upstream sources read during the implementation. Grapheme
segmentation uses a stdlib clusterer (Python has no ``Intl.Segmenter``) that
groups a base character with following combining marks / ZWJ / variation
selectors and pairs regional indicators — the cases the slice logic depends on.

Casing: snake_case identifiers, ``AnsiCode`` dataclass (PascalCase). ANSI byte
sequences are protocol literals, not wire-key dicts.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from wcwidth import wcwidth

# --- ANSI control-character constants --------------------

_BEL = "\x07"
_ESC = "\x1b"
_BACKSLASH = "\\"
_CSI = "["
_OSC = "]"
_C1_ST = "\x9c"

_CC_BEL = ord(_BEL)
_CC_ESC = ord(_ESC)
_CC_BACKSLASH = ord(_BACKSLASH)
_CC_CSI = ord(_CSI)
_CC_OSC = ord(_OSC)
_CC_C1_ST = ord(_C1_ST)
_CC_M = ord("m")
_CC_SEMI = ord(";")

# Escape code points (\x1b and the 8-bit CSI 0x9b).
_ESCAPES = frozenset({_CC_ESC, 0x9B})

# OSC 8 hyperlink constants.
_LINK_CODE_PREFIX = f"{_ESC}{_OSC}8;"
_LINK_CODE_PREFIX_CHAR_CODES = [ord(c) for c in _LINK_CODE_PREFIX]
_LINK_END_CODE = f"{_ESC}{_OSC}8;;{_BEL}"
_LINK_END_CODE_ST = f"{_ESC}{_OSC}8;;{_ESC}{_BACKSLASH}"
_LINK_END_CODE_C1ST = f"{_ESC}{_OSC}8;;{_C1_ST}"


# --- ansi-styles start->end code map -----------
# Generated from `ansi-styles`: each (start, end) SGR numeric pair. `color.ansi`
# wraps a number n as `\x1b[{n}m`.

_ANSI_STYLE_CODE_PAIRS: list[tuple[int, int]] = [
    (0, 0), (1, 22), (2, 22), (3, 23), (4, 24), (53, 55), (7, 27), (8, 28),
    (9, 29), (30, 39), (31, 39), (32, 39), (33, 39), (34, 39), (35, 39),
    (36, 39), (37, 39), (90, 39), (91, 39), (92, 39), (93, 39), (94, 39),
    (95, 39), (96, 39), (97, 39), (40, 49), (41, 49), (42, 49), (43, 49),
    (44, 49), (45, 49), (46, 49), (47, 49), (100, 49), (101, 49), (102, 49),
    (103, 49), (104, 49), (105, 49), (106, 49), (107, 49),
]


def _sgr(n: int) -> str:
    """``ansi-styles.color.ansi(n)`` — wrap a numeric SGR code as ``\\x1b[{n}m``."""
    return f"\x1b[{n}m"


# Numeric start -> numeric end (ansi-styles `codes` Map).
_CODES_MAP_NUM: dict[int, int] = {start: end for start, end in _ANSI_STYLE_CODE_PAIRS}
# String start-code -> string end-code; the set of all end codes.
_END_CODES_MAP: dict[str, str] = {_sgr(s): _sgr(e) for s, e in _ANSI_STYLE_CODE_PAIRS}
_END_CODES_SET: frozenset[str] = frozenset(_sgr(e) for _s, e in _ANSI_STYLE_CODE_PAIRS)

_COLOR_CLOSE = _sgr(39)  # ansi-styles.color.close
_BG_COLOR_CLOSE = _sgr(49)  # ansi-styles.bgColor.close
_RESET_OPEN = _sgr(0)  # ansi-styles.reset.open
_BOLD_OPEN = _sgr(1)  # ansi-styles.bold.open
_DIM_OPEN = _sgr(2)  # ansi-styles.dim.open


@dataclass
class AnsiCode:
    """An ANSI style/link token (TS ``AnsiCode``: ``{type:'ansi', code, endCode}``)."""

    code: str
    end_code: str


@dataclass
class _Token:
    """Tokenizer output item.

    ``type`` is one of ``"ansi"`` (style/link, paired), ``"control"`` (a
    self-contained OSC sequence with no end code), or ``"char"`` (a grapheme).
    ``ansi`` tokens carry ``code``/``end_code``; ``char`` tokens carry
    ``value``/``full_width``.
    """

    type: str
    code: str | None = None
    end_code: str | None = None
    value: str | None = None
    full_width: bool = False


# --- getEndCode ---------------------------------------


def _get_end_code(code: str) -> str:
    """Return the closing code for a start code."""
    if code in _END_CODES_SET:
        return code
    if code in _END_CODES_MAP:
        return _END_CODES_MAP[code]

    # Links.
    if code.startswith(_LINK_CODE_PREFIX):
        if code.endswith("\x1b\\"):
            return _LINK_END_CODE_ST
        if code.endswith("\x9c"):
            return _LINK_END_CODE_C1ST
        return _LINK_END_CODE  # BEL (\x07)

    body = code[2:]
    # 8-bit / 24-bit colors.
    if body.startswith("38"):
        return _COLOR_CLOSE
    if body.startswith("48"):
        return _BG_COLOR_CLOSE

    # Otherwise find the reset code in the ansi-styles map.
    try:
        num = int(body[:-1]) if body.endswith("m") else int(body)
    except ValueError:
        return _RESET_OPEN
    ret = _CODES_MAP_NUM.get(num)
    if ret is not None:
        return _sgr(ret)
    return _RESET_OPEN


def _ansi_codes_to_string(codes: list[AnsiCode]) -> str:
    """Join code strings after dedup."""
    deduplicated = list(dict.fromkeys(c.code for c in codes))
    return "".join(deduplicated)


def _is_intensity_code(code: AnsiCode) -> bool:
    """Whether ``code`` is bold/dim (both close with 22m but can coexist)."""
    return code.code == _BOLD_OPEN or code.code == _DIM_OPEN


# --- reduceAnsiCodes / undoAnsiCodes -----------


def _reduce_ansi_codes(codes: list[AnsiCode]) -> list[AnsiCode]:
    """Reduce to the minimum codes needed to render the same style."""
    ret: list[AnsiCode] = []
    for code in codes:
        if code.code == _RESET_OPEN:
            # Reset code, disable all codes.
            ret = []
        elif code.code in _END_CODES_SET:
            # End code, disable all matching start codes.
            ret = [rc for rc in ret if rc.end_code != code.code]
        else:
            # Start code. Remove codes it "overrides", then add it.
            if _is_intensity_code(code):
                # Intensity codes (1m, 2m) can coexist; add only if not present.
                if not any(
                    rc.code == code.code and rc.end_code == code.end_code for rc in ret
                ):
                    ret.append(code)
            else:
                ret = [rc for rc in ret if rc.end_code != code.end_code]
                ret.append(code)
    return ret


def _undo_ansi_codes(codes: list[AnsiCode]) -> list[AnsiCode]:
    """Codes needed to undo the given codes."""
    reduced = _reduce_ansi_codes(codes)
    return [AnsiCode(code=c.end_code, end_code=c.end_code) for c in reversed(reduced)]


# --- grapheme segmentation + width -------------------------------------------


def _is_extend(cp: int) -> bool:
    """Whether code point ``cp`` extends the preceding base in a grapheme.

    Covers combining marks (Mn/Mc/Me), ZWJ, and variation selectors — the
    "extend"/"ZWJ" classes that ``Intl.Segmenter`` glues to a base character.
    """
    if cp == 0x200D:  # ZERO WIDTH JOINER
        return True
    if 0xFE00 <= cp <= 0xFE0F:  # variation selectors
        return True
    if 0xE0100 <= cp <= 0xE01EF:  # variation selectors supplement
        return True
    cat = unicodedata.category(chr(cp))
    return cat in ("Mn", "Mc", "Me")


def _is_regional_indicator(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _segment_graphemes(s: str) -> list[str]:
    """Split ``s`` into grapheme clusters (stdlib stand-in for Intl.Segmenter).

    Groups a base code point with following extend characters (combining marks,
    ZWJ, variation selectors) and pairs consecutive regional indicators. This
    covers the clusters the slice logic relies on (Devanagari matras, emoji ZWJ
    sequences, flags). ANSI escape characters are returned as single-char
    segments so the tokenizer can detect them.
    """
    out: list[str] = []
    chars = list(s)
    i = 0
    n = len(chars)
    while i < n:
        ch = chars[i]
        cp = ord(ch)
        # Keep ANSI escape lead bytes isolated so the tokenizer sees them at a
        # segment boundary (Intl.Segmenter would split on them too).
        if cp in _ESCAPES:
            out.append(ch)
            i += 1
            continue
        cluster = ch
        i += 1
        # Regional indicator pairing: two RIs form one flag grapheme.
        if _is_regional_indicator(cp) and i < n and _is_regional_indicator(ord(chars[i])):
            cluster += chars[i]
            i += 1
        # Absorb following extend characters (and ZWJ + following base).
        while i < n:
            nxt = ord(chars[i])
            if _is_extend(nxt):
                cluster += chars[i]
                i += 1
                # A ZWJ glues the next base too (emoji ZWJ sequences).
                if nxt == 0x200D and i < n and not _is_extend(ord(chars[i])):
                    cluster += chars[i]
                    i += 1
            else:
                break
        out.append(cluster)
    return out


def _is_fullwidth_code_point(cp: int) -> bool:
    """East-Asian wide/fullwidth.

    The upstream lib returns ``isFullWidth(cp) || isWide(cp)``; ``wcwidth`` of a
    bare code point is 2 for exactly that set (verified against the JS lib).
    """
    return wcwidth(chr(cp)) == 2


def _is_fullwidth_grapheme(grapheme: str, base_code_point: int) -> bool:
    """Return whether fullwidth grapheme."""
    if _is_fullwidth_code_point(base_code_point):
        return True
    # Variation Selector 16 forces emoji presentation (2 columns wide).
    if "️" in grapheme:
        return True
    # Regional indicator pairs form flag emoji (2 columns wide).
    if 0x1F1E6 <= base_code_point <= 0x1F1FF:
        return True
    return False


def string_width(s: str) -> int:
    """Display width of ``s`` in terminal cells (ambiguousAsWide: false).

    Faithful slice of ``src/utils/terminal/stringWidth.ts`` sufficient for
    ``slice_ansi`` (it only ever measures single-grapheme token values here):
    sum each grapheme's first non-zero-width character's wcwidth; zero-width and
    combining characters contribute 0.
    """
    if not isinstance(s, str) or len(s) == 0:
        return 0
    width = 0
    for grapheme in _segment_graphemes(s):
        cp0 = ord(grapheme[0])
        if _is_fullwidth_grapheme(grapheme, cp0):
            width += 2
            continue
        for ch in grapheme:
            w = wcwidth(ch)
            if w is not None and w > 0:
                width += w
                break
    return width


# --- tokenizer -----------------------------------------


def _find_osc_terminator_index(string: str, start_index: int) -> int:
    """Index of the last char of the first OSC terminator at/after start_index."""
    i = start_index
    length = len(string)
    while i < length:
        ch = ord(string[i])
        if ch == _CC_BEL:
            return i
        if ch == _CC_C1_ST:
            return i
        if ch == _CC_ESC and i + 1 < length and ord(string[i + 1]) == _CC_BACKSLASH:
            return i + 1
        i += 1
    return -1


def _parse_link_code(string: str, offset: int) -> str | None:
    """Parse an OSC 8 hyperlink sequence starting at ``offset`` (or ``None``)."""
    string = string[offset:]
    for index in range(1, len(_LINK_CODE_PREFIX_CHAR_CODES)):
        if index >= len(string) or ord(string[index]) != _LINK_CODE_PREFIX_CHAR_CODES[index]:
            return None
    params_end_index = string.find(";", len(_LINK_CODE_PREFIX))
    if params_end_index == -1:
        return None
    end_index = _find_osc_terminator_index(string, params_end_index + 1)
    if end_index == -1:
        return None
    return string[: end_index + 1]


def _parse_osc_sequence(string: str, offset: int) -> str | None:
    """Parse a generic (non-link) OSC sequence (window title, etc.)."""
    string = string[offset:]
    end_index = _find_osc_terminator_index(string, 2)
    if end_index == -1:
        return None
    return string[: end_index + 1]


def _find_sgr_sequence_end_index(string: str) -> int:
    """Index of the last char of an SGR sequence like ``\\x1b[38;2;...m``."""
    for index in range(2, len(string)):
        char_code = ord(string[index])
        if char_code == _CC_M:
            return index
        if char_code == _CC_SEMI:
            continue
        if ord("0") <= char_code <= ord("9"):
            continue
        break
    return -1


def _parse_sgr_sequence(string: str, offset: int) -> str | None:
    """Parse an SGR sequence starting at ``offset`` (or ``None``)."""
    string = string[offset:]
    end_index = _find_sgr_sequence_end_index(string)
    if end_index == -1:
        return None
    return string[: end_index + 1]


def _split_compound_sgr_sequences(code: str) -> list[str]:
    """Split compound SGR like ``\\x1b[1;3;31m`` into individual components."""
    if ";" not in code:
        return [code]
    code_parts = code[2:-1].split(";")
    ret: list[str] = []
    i = 0
    while i < len(code_parts):
        raw_code = code_parts[i]
        # Keep 8-bit / 24-bit color codes (multiple ";") together.
        if raw_code in ("38", "48"):
            if i + 2 < len(code_parts) and code_parts[i + 1] == "5":
                ret.append(";".join(code_parts[i : i + 3]))
                i += 3
                continue
            if i + 4 < len(code_parts) and code_parts[i + 1] == "2":
                ret.append(";".join(code_parts[i : i + 5]))
                i += 5
                continue
        ret.append(raw_code)
        i += 1
    return [f"\x1b[{part}m" for part in ret]


def _code_point_at(string: str, index: int) -> int | None:
    """Code point at UTF-16-style ``index`` within ``string`` (or ``None``).

    The TS tokenizer indexes by JS string (UTF-16 code units). Python strings
    index by code point, but the tokenizer only peeks one position ahead for
    ASCII-range markers (``]``/``[``), so a code-point index is equivalent for
    the bytes that matter here.
    """
    if index < 0 or index >= len(string):
        return None
    return ord(string[index])


def tokenize(string: str) -> list[_Token]:
    """Tokenize ``string`` into ANSI / control / char tokens.

    The TS ``endChar`` parameter is intentionally not threaded — ``slice_ansi``
    never passes it (it tracks display cells separately), matching the comment in
    the TS source.
    """
    ret: list[_Token] = []
    segments = _segment_graphemes(string)
    code_end_index = 0
    char_index = 0  # running index into the original string (code points)
    for segment in segments:
        index = char_index
        char_index += len(segment)
        # Skip segments consumed as part of an ANSI sequence.
        if index < code_end_index:
            continue
        code_point = ord(segment[0])
        if code_point in _ESCAPES:
            code: str | None = None
            next_code_point = _code_point_at(string, index + 1)
            if next_code_point == _CC_OSC:
                code = _parse_link_code(string, index)
                if code:
                    ret.append(
                        _Token(type="ansi", code=code, end_code=_get_end_code(code))
                    )
                else:
                    code = _parse_osc_sequence(string, index)
                    if code:
                        ret.append(_Token(type="control", code=code))
            elif next_code_point == _CC_CSI:
                code = _parse_sgr_sequence(string, index)
                if code:
                    for individual_code in _split_compound_sgr_sequences(code):
                        ret.append(
                            _Token(
                                type="ansi",
                                code=individual_code,
                                end_code=_get_end_code(individual_code),
                            )
                        )
            if code:
                code_end_index = index + len(code)
                continue
        full_width = _is_fullwidth_grapheme(segment, code_point)
        ret.append(_Token(type="char", value=segment, full_width=full_width))
    return ret


# --- slice_ansi ---------------------------------------


def _is_end_code(code: AnsiCode) -> bool:
    """A code is an "end code" if its code equals its end_code (e.g. link close)."""
    return code.code == code.end_code


def _filter_start_codes(codes: list[AnsiCode]) -> list[AnsiCode]:
    """Keep only "start codes" (drop end codes)."""
    return [c for c in codes if not _is_end_code(c)]


def slice_ansi(string: str, start: int, end: int | None = None) -> str:
    """Slice a string containing ANSI escape codes by display cells.

    Properly handles OSC 8 hyperlink sequences (via the existing tokenizer).
    ``start``/``end`` are measured in terminal display cells; combining marks are
    width 0 and stay attached to their base character.
    """
    # Don't pass `end` to tokenize — it counts code units, not display cells.
    tokens = tokenize(string)
    active_codes: list[AnsiCode] = []
    position = 0
    result = ""
    include = False

    for token in tokens:
        # Advance by display width, not code units.
        if token.type == "ansi":
            width = 0
        elif token.full_width:
            width = 2
        else:
            width = string_width(token.value or "")

        # Break AFTER trailing zero-width marks (see TS comment). ANSI codes are
        # width 0 but must NOT be included past `end`. The `not include` guard
        # keeps empty slices (start == end) empty even when the string starts
        # with a zero-width char.
        if end is not None and position >= end:
            if token.type == "ansi" or width > 0 or not include:
                break

        if token.type == "ansi":
            active_codes.append(AnsiCode(code=token.code or "", end_code=token.end_code or ""))
            if include:
                # Emit all ANSI codes during the slice.
                result += token.code or ""
        else:
            if not include and position >= start:
                # Skip leading zero-width marks at the start boundary — they
                # belong to the preceding base char in the left half.
                if start > 0 and width == 0:
                    continue
                include = True
                # Reduce and filter to only active start codes.
                active_codes = _filter_start_codes(_reduce_ansi_codes(active_codes))
                result = _ansi_codes_to_string(active_codes)

            if include:
                # Faithful to the TS: `char` tokens carry a real `value`;
                # `control` tokens (non-link OSC) have no `value`, and JS
                # `result += undefined` coerces to the literal "undefined".
                result += token.value if token.value is not None else "undefined"

            position += width

    # Only undo start codes that are still active.
    active_start_codes = _filter_start_codes(_reduce_ansi_codes(active_codes))
    result += _ansi_codes_to_string(_undo_ansi_codes(active_start_codes))
    return result
