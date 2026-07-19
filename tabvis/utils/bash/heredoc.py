"""Heredoc extraction and restoration utilities.


The shell-quote library parses ``<<`` as two separate ``<`` redirect operators, which breaks
command splitting for heredoc syntax. This module extracts heredocs before parsing (replacing
them with random-salted placeholders) and restores them after.

Supported heredoc variations:

- ``<<WORD``      basic heredoc
- ``<<'WORD'``    single-quoted delimiter (no variable expansion in content)
- ``<<"WORD"``    double-quoted delimiter (with variable expansion)
- ``<<-WORD``     dash prefix (strips leading tabs from content)
- ``<<-'WORD'``   combined dash and quoted delimiter

The ``HeredocInfo`` / ``HeredocExtractionResult`` data shapes are plain dataclasses. Index
fields are positions into the (same) command string being sliced — JS string indices,
preserved verbatim (the parser's UTF-8 byte-offset convention does not apply here; these are
slice offsets into the command we also slice with).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

__all__ = [
    "HeredocInfo",
    "HeredocExtractionResult",
    "extract_heredocs",
    "restore_heredocs",
    "contains_heredoc",
]

_HEREDOC_PLACEHOLDER_PREFIX = "__HEREDOC_"
_HEREDOC_PLACEHOLDER_SUFFIX = "__"


def _generate_placeholder_salt() -> str:
    """8 random bytes as hex (16 chars). Prevents collisions with literal ``__HEREDOC_N__``."""
    return secrets.token_hex(8)


# Regex for matching heredoc start syntax. Two alternatives handle quoted vs unquoted
# delimiters: group 2 = opening quote, group 3 = quoted delimiter word (may include a leading
# backslash), group 4 = unquoted delimiter word. Uses ``[ \t]*`` (not ``\s*``) to avoid
# matching across newlines.
_HEREDOC_START_PATTERN = re.compile(
    r"(?<!<)<<(?!<)(-)?[ \t]*(?:(['\"])(\\?\w+)\2|\\?(\w+))"
)


@dataclass
class HeredocInfo:
    """One extracted heredoc. Field names match the TS ``HeredocInfo`` verbatim."""

    # The full heredoc text including << operator, delimiter, content, and closing delimiter.
    full_text: str
    # The delimiter word (without quotes).
    delimiter: str
    # Start position of the << operator in the original command.
    operator_start_index: int
    # End position of the << operator (exclusive).
    operator_end_index: int
    # Start position of heredoc content (the newline before content).
    content_start_index: int
    # End position of heredoc content including closing delimiter (exclusive).
    content_end_index: int


@dataclass
class HeredocExtractionResult:
    # The command with heredocs replaced by placeholders.
    processed_command: str
    # Map of placeholder string to original heredoc info.
    heredocs: dict[str, HeredocInfo] = field(default_factory=dict)


_DOLLAR_QUOTE_RE = re.compile(r"\$['\"]")
_ARITH_OPEN_RE = re.compile(r"\(\(")
_ARITH_CLOSE_RE = re.compile(r"\)\)")
_WORD_TERMINATOR_RE = re.compile(r"^[ \t\n|&;()<>]$")
_EOF_METACHAR_RE = re.compile(r"^[)}`|&;(<>]$")
_LEADING_TABS_RE = re.compile(r"^\t*")


def extract_heredocs(
    command: str,
    options: dict | None = None,
) -> HeredocExtractionResult:
    """Extract heredocs from ``command``, replacing them with salted placeholders.

    ``options`` may contain ``{'quotedOnly': bool}``. Returns a
    :class:`HeredocExtractionResult` with the processed command and a placeholder→info map.
    """
    quoted_only = bool(options and options.get("quotedOnly"))
    heredocs: dict[str, HeredocInfo] = {}

    # Quick check: if no << present, skip processing.
    if "<<" not in command:
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Security: bail on $'...' / $"..." — our quote tracker doesn't handle the $ prefix.
    if _DOLLAR_QUOTE_RE.search(command):
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Bail on backticks before the first << (complex parsing / early-closure rules).
    first_heredoc_pos = command.find("<<")
    if first_heredoc_pos > 0 and "`" in command[:first_heredoc_pos]:
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Security: bail on arithmetic `((` before the first `<<` without a matching `))`.
    if first_heredoc_pos > 0:
        before_heredoc = command[:first_heredoc_pos]
        open_arith = len(_ARITH_OPEN_RE.findall(before_heredoc))
        close_arith = len(_ARITH_CLOSE_RE.findall(before_heredoc))
        if open_arith > close_arith:
            return HeredocExtractionResult(
                processed_command=command, heredocs=heredocs
            )

    heredoc_matches: list[HeredocInfo] = []
    skipped_heredoc_ranges: list[dict[str, int]] = []

    # Incremental quote/comment scanner state (see TS commentary for exact semantics).
    scan_state = {
        "pos": 0,
        "in_single": False,
        "in_double": False,
        "in_comment": False,
        "dq_escape_next": False,
        "pending_backslashes": 0,
    }

    def advance_scan(target: int) -> None:
        i = scan_state["pos"]
        while i < target:
            ch = command[i]

            # Any physical newline clears comment state (quote-blind, like the old helper).
            if ch == "\n":
                scan_state["in_comment"] = False

            if scan_state["in_single"]:
                if ch == "'":
                    scan_state["in_single"] = False
                i += 1
                continue

            if scan_state["in_double"]:
                if scan_state["dq_escape_next"]:
                    scan_state["dq_escape_next"] = False
                    i += 1
                    continue
                if ch == "\\":
                    scan_state["dq_escape_next"] = True
                    i += 1
                    continue
                if ch == '"':
                    scan_state["in_double"] = False
                i += 1
                continue

            # Unquoted context. Quote tracking is COMMENT-BLIND.
            if ch == "\\":
                scan_state["pending_backslashes"] += 1
                i += 1
                continue
            escaped = scan_state["pending_backslashes"] % 2 == 1
            scan_state["pending_backslashes"] = 0
            if escaped:
                i += 1
                continue

            if ch == "'":
                scan_state["in_single"] = True
            elif ch == '"':
                scan_state["in_double"] = True
            elif not scan_state["in_comment"] and ch == "#":
                scan_state["in_comment"] = True
            i += 1
        scan_state["pos"] = target

    for match in _HEREDOC_START_PATTERN.finditer(command):
        start_index = match.start()

        advance_scan(start_index)

        # Skip if this << is inside a quoted string.
        if scan_state["in_single"] or scan_state["in_double"]:
            continue

        # Skip if inside a comment.
        if scan_state["in_comment"]:
            continue

        # Skip if preceded by an odd number of backslashes (`\<<EOF` is not a heredoc).
        if scan_state["pending_backslashes"] % 2 == 1:
            continue

        # Bail if this << falls inside a previously SKIPPED heredoc's body.
        inside_skipped = False
        for skipped in skipped_heredoc_ranges:
            if (
                start_index > skipped["content_start_index"]
                and start_index < skipped["content_end_index"]
            ):
                inside_skipped = True
                break
        if inside_skipped:
            continue

        full_match = match.group(0)
        is_dash = match.group(1) == "-"
        # group 3 = quoted delimiter (may include backslash), group 4 = unquoted.
        delimiter = match.group(3) if match.group(3) is not None else match.group(4)
        operator_end_index = start_index + len(full_match)

        # Check 1: if a quote was captured, verify the closing quote was matched.
        quote_char = match.group(2)
        if quote_char and (
            operator_end_index - 1 >= len(command)
            or command[operator_end_index - 1] != quote_char
        ):
            continue

        is_escaped_delimiter = "\\" in full_match
        is_quoted_or_escaped = bool(quote_char) or is_escaped_delimiter

        # Check 2: next char after the match must be a bash word terminator (or EOS).
        if operator_end_index < len(command):
            next_char = command[operator_end_index]
            if not _WORD_TERMINATOR_RE.match(next_char):
                continue

        # Find the first newline NOT inside a quoted string (logical line end).
        first_newline_offset = -1
        in_single_quote = False
        in_double_quote = False
        k = operator_end_index
        while k < len(command):
            ch = command[k]
            if in_single_quote:
                if ch == "'":
                    in_single_quote = False
                k += 1
                continue
            if in_double_quote:
                if ch == "\\":
                    k += 1  # skip escaped char inside double quotes
                    k += 1
                    continue
                if ch == '"':
                    in_double_quote = False
                k += 1
                continue
            # Unquoted context.
            if ch == "\n":
                first_newline_offset = k - operator_end_index
                break
            backslash_count = 0
            j = k - 1
            while j >= operator_end_index and command[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 1:
                k += 1
                continue  # escaped char
            if ch == "'":
                in_single_quote = True
            elif ch == '"':
                in_double_quote = True
            k += 1

        # If no unquoted newline found, this heredoc has no content - skip it.
        if first_newline_offset == -1:
            continue

        # Bail on backslash-newline continuation at the end of same-line content.
        same_line_content = command[
            operator_end_index : operator_end_index + first_newline_offset
        ]
        trailing_backslashes = 0
        for j in range(len(same_line_content) - 1, -1, -1):
            if same_line_content[j] == "\\":
                trailing_backslashes += 1
            else:
                break
        if trailing_backslashes % 2 == 1:
            continue

        content_start_index = operator_end_index + first_newline_offset
        after_newline = command[content_start_index + 1 :]  # +1 to skip the newline itself
        content_lines = after_newline.split("\n")

        # Find the closing delimiter — must be on its own line.
        closing_line_index = -1
        for idx, line in enumerate(content_lines):
            if is_dash:
                stripped = _LEADING_TABS_RE.sub("", line)
                if stripped == delimiter:
                    closing_line_index = idx
                    break
            else:
                if line == delimiter:
                    closing_line_index = idx
                    break

            # PST_EOFTOKEN-like early closure / metacharacter-after-delimiter bail.
            eof_check_line = _LEADING_TABS_RE.sub("", line) if is_dash else line
            if len(eof_check_line) > len(delimiter) and eof_check_line.startswith(
                delimiter
            ):
                char_after_delimiter = eof_check_line[len(delimiter)]
                if _EOF_METACHAR_RE.match(char_after_delimiter):
                    closing_line_index = -1
                    break

        # quotedOnly: record the unquoted heredoc's body range for nesting checks, then skip.
        if quoted_only and not is_quoted_or_escaped:
            if closing_line_index == -1:
                skip_content_end_index = len(command)
            else:
                skip_lines_up_to_closing = content_lines[: closing_line_index + 1]
                skip_content_length = len("\n".join(skip_lines_up_to_closing))
                skip_content_end_index = (
                    content_start_index + 1 + skip_content_length
                )
            skipped_heredoc_ranges.append(
                {
                    "content_start_index": content_start_index,
                    "content_end_index": skip_content_end_index,
                }
            )
            continue

        # If no closing delimiter found, this is malformed - skip it.
        if closing_line_index == -1:
            continue

        lines_up_to_closing = content_lines[: closing_line_index + 1]
        content_length = len("\n".join(lines_up_to_closing))
        content_end_index = content_start_index + 1 + content_length

        # Bail if this heredoc's content range OVERLAPS any previously-skipped range.
        overlaps_skipped = False
        for skipped in skipped_heredoc_ranges:
            if (
                content_start_index < skipped["content_end_index"]
                and skipped["content_start_index"] < content_end_index
            ):
                overlaps_skipped = True
                break
        if overlaps_skipped:
            continue

        operator_text = command[start_index:operator_end_index]
        content_text = command[content_start_index:content_end_index]
        full_text = operator_text + content_text

        heredoc_matches.append(
            HeredocInfo(
                full_text=full_text,
                delimiter=delimiter,
                operator_start_index=start_index,
                operator_end_index=operator_end_index,
                content_start_index=content_start_index,
                content_end_index=content_end_index,
            )
        )

    if len(heredoc_matches) == 0:
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Filter out nested heredocs — any whose operator starts inside another's content range.
    top_level_heredocs: list[HeredocInfo] = []
    for candidate in heredoc_matches:
        nested = False
        for other in heredoc_matches:
            if candidate is other:
                continue
            if (
                candidate.operator_start_index > other.content_start_index
                and candidate.operator_start_index < other.content_end_index
            ):
                nested = True
                break
        if not nested:
            top_level_heredocs.append(candidate)

    if len(top_level_heredocs) == 0:
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Multiple heredocs sharing a content start position → index corruption; bail.
    content_start_positions = {h.content_start_index for h in top_level_heredocs}
    if len(content_start_positions) < len(top_level_heredocs):
        return HeredocExtractionResult(processed_command=command, heredocs=heredocs)

    # Sort by content end position descending so we replace from end to start.
    top_level_heredocs.sort(key=lambda h: h.content_end_index, reverse=True)

    salt = _generate_placeholder_salt()

    processed_command = command
    for index, info in enumerate(top_level_heredocs):
        placeholder_index = len(top_level_heredocs) - 1 - index
        placeholder = (
            f"{_HEREDOC_PLACEHOLDER_PREFIX}{placeholder_index}_{salt}"
            f"{_HEREDOC_PLACEHOLDER_SUFFIX}"
        )

        heredocs[placeholder] = info

        processed_command = (
            processed_command[: info.operator_start_index]
            + placeholder
            + processed_command[info.operator_end_index : info.content_start_index]
            + processed_command[info.content_end_index :]
        )

    return HeredocExtractionResult(
        processed_command=processed_command, heredocs=heredocs
    )


def _restore_heredocs_in_string(
    text: str,
    heredocs: dict[str, HeredocInfo],
) -> str:
    result = text
    for placeholder, info in heredocs.items():
        result = result.replace(placeholder, info.full_text)
    return result


def restore_heredocs(
    parts: list[str],
    heredocs: dict[str, HeredocInfo],
) -> list[str]:
    """Restore heredoc placeholders in an array of strings."""
    if len(heredocs) == 0:
        return parts
    return [_restore_heredocs_in_string(part, heredocs) for part in parts]


def contains_heredoc(command: str) -> bool:
    """Quick (non-validating) check whether a command appears to contain heredoc syntax."""
    return _HEREDOC_START_PATTERN.search(command) is not None
