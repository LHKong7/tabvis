"""Structured line diff

The TS module wraps the npm ``diff`` package's ``structuredPatch`` to build the
hunk arrays that ``FileEditTool``'s result display renders. The npm ``diff``
output has a very specific shape that ``difflib`` does **not** reproduce out of
the box:

* lines are prefixed with ``' '`` (context), ``'-'`` (removed), ``'+'`` (added);
* a literal ``'\\ No newline at end of file'`` marker is spliced in after the
  last line of a side that lacks a trailing newline;
* a from-empty hunk reports ``oldStart=1, oldLines=0`` (and ``newStart=1,
  newLines=0`` for a to-empty hunk); identical inputs return ``[]``;
* adjacent change groups merge into one hunk when ``<= 2*context`` unchanged
  lines separate them (the standard unified-diff window rule).

So this implementation reimplements ``structuredPatch`` directly on top of
``difflib.SequenceMatcher`` (whose ``get_grouped_opcodes(n)`` window rule
matches the ``diff`` library exactly) plus a faithful reproduction of the
``diffLinesResultToPatch`` line-emit + trailing-newline cleanup passes. The
returned hunks are plain ``dict`` objects keyed with the TS/Anthropic wire keys
(``oldStart``/``oldLines``/``newStart``/``newLines``/``lines``) so they
round-trip through the transcript / SDK output unchanged.

Casing convention: Python identifiers (functions, locals) are snake_case;
the hunk dicts keep their camelCase wire keys.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict


# A single hunk in the structured patch. Wire keys (camelCase) are preserved
# because these dicts round-trip to the FileEditTool result / transcript.
StructuredPatchHunk = dict[str, Any]


class FileEdit(TypedDict, total=False):
    """Runtime edit shape consumed by :func:`get_patch_for_display`.

    Mirrors ``src/tools/FileEditTool/types.ts`` ``FileEdit`` — ``replace_all`` is
    optional here (defaults to ``False``) to match the TS ``'replace_all' in edit``
    guard rather than requiring callers to always supply it.
    """

    old_string: str
    new_string: str
    replace_all: bool


CONTEXT_LINES = 3
DIFF_TIMEOUT_MS = 5_000

# For some reason, & confuses the diff library, so we replace it with a token,
# then substitute it back in after the diff is computed. ($ similarly trips up
# JS replacement-pattern handling.) Kept for byte-parity with the TS escape pass.
AMPERSAND_TOKEN = "<<:AMPERSAND_TOKEN:>>"
DOLLAR_TOKEN = "<<:DOLLAR_TOKEN:>>"

_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def escape_for_diff(s: str) -> str:
    return s.replace("&", AMPERSAND_TOKEN).replace("$", DOLLAR_TOKEN)


def unescape_from_diff(s: str) -> str:
    return s.replace(AMPERSAND_TOKEN, "&").replace(DOLLAR_TOKEN, "$")


def convert_leading_tabs_to_spaces(content: str) -> str:
    """Render each leading tab on a line as two spaces

    Skips the regex entirely for the common tab-free case (matching the TS
    fast-path), so tab-free content is returned unchanged.
    """
    if "\t" not in content:
        return content
    return re.sub(r"^\t+", lambda m: "  " * len(m.group(0)), content, flags=re.MULTILINE)


def adjust_hunk_line_numbers(
    hunks: list[StructuredPatchHunk], offset: int
) -> list[StructuredPatchHunk]:
    """Shift hunk line numbers by ``offset``.

    Use when :func:`get_patch_for_display` received a slice of the file rather
    than the whole file; callers pass ``ctx.line_offset - 1`` to convert
    slice-relative line numbers to file-relative ones.
    """
    if offset == 0:
        return hunks
    return [
        {
            **h,
            "oldStart": h["oldStart"] + offset,
            "newStart": h["newStart"] + offset,
        }
        for h in hunks
    ]


# --- core structuredPatch reimplementation ------------------------------------


def _tokenize(value: str) -> list[str]:
    """Split ``value`` into line tokens that retain their trailing newline.

        ``["a\\n", "b\\n", "c"]`` and ``"a\\nb\\n"`` -> ``["a\\n", "b\\n"]`` (the
    final empty token after a trailing newline is dropped). An empty string
    yields ``[]``.
    """
    if value == "":
        return []
    # split keeping the separators; re.split with a capture group interleaves
    # content and separators just like JS ``split(/(\n|\r\n)/)``.
    parts = re.split(r"(\r\n|\n)", value)
    # Drop the trailing empty token produced when the string ends with a newline.
    if parts and parts[-1] == "":
        parts.pop()
    tokens: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # separator: merge onto the preceding content token
            tokens[-1] += part
        else:
            tokens.append(part)
    return tokens


def _equal_key(token: str, ignore_whitespace: bool) -> str:
    if ignore_whitespace:
        return token.strip()
    return token


def _structured_patch(
    old_str: str,
    new_str: str,
    *,
    context: int,
    ignore_whitespace: bool,
) -> list[StructuredPatchHunk]:
    """Reimplementation of npm ``diff``'s ``structuredPatch`` (single file).

    Reproduces both passes of ``diffLinesResultToPatch``:
    1. build hunks with newline-bearing line tokens and the standard
       ``<= 2*context`` overlap/merge window;
    2. strip the trailing ``\\n`` from each emitted line and splice in
       ``'\\ No newline at end of file'`` where a side lacked one.
    """
    import difflib

    old_tokens = _tokenize(old_str)
    new_tokens = _tokenize(new_str)

    old_keys = [_equal_key(t, ignore_whitespace) for t in old_tokens]
    new_keys = [_equal_key(t, ignore_whitespace) for t in new_tokens]

    matcher = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    opcodes = matcher.get_opcodes()

    # Translate opcodes into the ``diff`` library's change-object stream so we
    # can run its exact patch-emitting loop. Each change carries (kind, lines)
    # where kind in {'equal','-','+'} and lines are original (untrimmed) tokens.
    changes: list[tuple[str, list[str]]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            # Context content comes from the old side (matches the diff lib,
            # whose unchanged change-object value is the common/old content).
            changes.append(("equal", old_tokens[i1:i2]))
        elif tag == "delete":
            changes.append(("-", old_tokens[i1:i2]))
        elif tag == "insert":
            changes.append(("+", new_tokens[j1:j2]))
        elif tag == "replace":
            # diff emits removals before additions for a replaced run.
            changes.append(("-", old_tokens[i1:i2]))
            changes.append(("+", new_tokens[j1:j2]))

    # Append an empty trailing change to make cleanup easier (as the TS does).
    changes.append(("equal", []))

    hunks: list[StructuredPatchHunk] = []
    old_range_start = 0
    new_range_start = 0
    cur_range: list[str] = []
    old_line = 1
    new_line = 1

    def context_lines(lines: list[str]) -> list[str]:
        return [" " + entry for entry in lines]

    n_changes = len(changes)
    for i in range(n_changes):
        kind, lines = changes[i]
        if kind in ("+", "-"):
            if not old_range_start:
                prev = changes[i - 1] if i - 1 >= 0 else None
                old_range_start = old_line
                new_range_start = new_line
                if prev is not None:
                    prev_lines = prev[1]
                    cur_range = (
                        context_lines(prev_lines[-context:]) if context > 0 else []
                    )
                    old_range_start -= len(cur_range)
                    new_range_start -= len(cur_range)
            prefix = "+" if kind == "+" else "-"
            for line in lines:
                cur_range.append(prefix + line)
            if kind == "+":
                new_line += len(lines)
            else:
                old_line += len(lines)
        else:
            # Identical context lines.
            if old_range_start:
                if len(lines) <= context * 2 and i < n_changes - 2:
                    # Overlapping — keep the range open.
                    for line in context_lines(lines):
                        cur_range.append(line)
                else:
                    # End the range and output the hunk.
                    context_size = min(len(lines), context)
                    for line in context_lines(lines[:context_size]):
                        cur_range.append(line)
                    hunks.append(
                        {
                            "oldStart": old_range_start,
                            "oldLines": old_line - old_range_start + context_size,
                            "newStart": new_range_start,
                            "newLines": new_line - new_range_start + context_size,
                            "lines": cur_range,
                        }
                    )
                    old_range_start = 0
                    new_range_start = 0
                    cur_range = []
            old_line += len(lines)
            new_line += len(lines)

    # Step 2: strip trailing '\n' from each line; splice in the no-newline marker.
    for hunk in hunks:
        hl = hunk["lines"]
        i = 0
        while i < len(hl):
            if hl[i].endswith("\n"):
                hl[i] = hl[i][:-1]
            else:
                hl.insert(i + 1, _NO_NEWLINE_MARKER)
                i += 1  # skip the marker we just inserted
            i += 1

    return hunks


def count_lines_changed(
    patch: list[StructuredPatchHunk], new_file_content: str | None = None
) -> None:
    """Count added/removed lines in a patch and report them.

    For new files (empty patch) pass the content string as ``new_file_content``
    to count every line as an addition. Side effects (LOC counter, cost tracker)
    remain stubbed until those modules are implemented.
    """
    num_additions = 0
    num_removals = 0

    if len(patch) == 0 and new_file_content:
        # For new files, count all lines as additions.
        num_additions = len(re.split(r"\r?\n", new_file_content))
    else:
        for hunk in patch:
            for line in hunk["lines"]:
                if line.startswith("+"):
                    num_additions += 1
                elif line.startswith("-"):
                    num_removals += 1

    # state.get_loc_counter once those modules are implemented. No-op for now.
    _add_to_total_lines_changed(num_additions, num_removals)


def _add_to_total_lines_changed(num_additions: int, num_removals: int) -> None:
    # src/bootstrap/state.ts (getLocCounter); currently a no-op sink.
    return None


def get_patch_from_contents(
    *,
    file_path: str,
    old_content: str,
    new_content: str,
    ignore_whitespace: bool = False,
    single_hunk: bool = False,
) -> list[StructuredPatchHunk]:
    """Structured diff between two content strings.

    ``single_hunk=True`` uses a 100_000-line context window so the whole file
    collapses into one hunk (matching the TS). The ampersand/dollar escape pass
    is preserved and reversed after diffing.
    """
    hunks = _structured_patch(
        escape_for_diff(old_content),
        escape_for_diff(new_content),
        context=100_000 if single_hunk else CONTEXT_LINES,
        ignore_whitespace=ignore_whitespace,
    )
    return [
        {**h, "lines": [unescape_from_diff(line) for line in h["lines"]]}
        for h in hunks
    ]


def get_patch_for_display(
    *,
    file_path: str,
    file_contents: str,
    edits: list[FileEdit],
    ignore_whitespace: bool = False,
) -> list[StructuredPatchHunk]:
    """Structured diff for display with ``edits`` applied to ``file_contents``.

    Leading tabs are rendered as spaces for display on both the original and the
    edited content. Edits are applied sequentially: ``replace_all`` replaces
    every occurrence, otherwise only the first. Returns the hunk list (lines
    unescaped).
    """
    prepared_file_contents = escape_for_diff(
        convert_leading_tabs_to_spaces(file_contents)
    )

    edited = prepared_file_contents
    for edit in edits:
        old_string = edit["old_string"]
        new_string = edit["new_string"]
        replace_all = edit.get("replace_all", False)
        escaped_old = escape_for_diff(convert_leading_tabs_to_spaces(old_string))
        escaped_new = escape_for_diff(convert_leading_tabs_to_spaces(new_string))
        if replace_all:
            edited = edited.replace(escaped_old, escaped_new)
        else:
            edited = edited.replace(escaped_old, escaped_new, 1)

    hunks = _structured_patch(
        prepared_file_contents,
        edited,
        context=CONTEXT_LINES,
        ignore_whitespace=ignore_whitespace,
    )
    return [
        {**h, "lines": [unescape_from_diff(line) for line in h["lines"]]}
        for h in hunks
    ]
