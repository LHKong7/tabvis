"""Lightweight parser for ``.git/config`` files

Verified against git's ``config.c``:
  - Section names: case-insensitive, alphanumeric + hyphen
  - Subsection names (quoted): case-sensitive, backslash escapes (``\\\\`` and ``\\"``)
  - Key names: case-insensitive, alphanumeric + hyphen
  - Values: optional quoting, inline comments (``#`` or ``;``), backslash escapes

Casing: Python identifiers are snake_case; this module returns/accepts plain ``str`` — no
wire-key dicts to preserve.

The git helpers use a flat-module layout under
``tabvis/utils/git_*.py`` siblings (NOT a ``tabvis/utils/git/`` package) because the existing
``tabvis/utils/git.py`` module would be shadowed by a same-named package dir (CPython resolves
``tabvis.utils.git`` to the package over the module). See memory ``tabvis-flat-tool-modules``.
"""

from __future__ import annotations

import os


async def parse_git_config_value(
    git_dir: str,
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    """Parse a single value from ``.git/config``.

    Finds the first matching key under the given section/subsection. Returns ``None`` when the
    config file can't be read (parity with the TS ``try/catch`` returning ``null``).
    """
    try:
        with open(os.path.join(git_dir, "config"), encoding="utf-8") as fh:
            config = fh.read()
    except OSError:
        return None
    return parse_config_string(config, section, subsection, key)


def parse_config_string(
    config: str,
    section: str,
    subsection: str | None,
    key: str,
) -> str | None:
    """Parse a config value from an in-memory config string. Exported for testing."""
    lines = config.split("\n")
    section_lower = section.lower()
    key_lower = key.lower()

    in_section = False
    for line in lines:
        trimmed = line.strip()

        # Skip empty lines and comment-only lines.
        if len(trimmed) == 0 or trimmed[0] == "#" or trimmed[0] == ";":
            continue

        # Section header.
        if trimmed[0] == "[":
            in_section = _matches_section_header(trimmed, section_lower, subsection)
            continue

        if not in_section:
            continue

        # Key-value line: find the key name.
        parsed = _parse_key_value(trimmed)
        if parsed is not None and parsed[0].lower() == key_lower:
            return parsed[1]

    return None


def _parse_key_value(line: str) -> tuple[str, str] | None:
    """Parse a ``key = value`` line. ``None`` if the line has no valid key."""
    # Read key: alphanumeric + hyphen.
    i = 0
    n = len(line)
    while i < n and _is_key_char(line[i]):
        i += 1
    if i == 0:
        return None
    key = line[:i]

    # Skip whitespace.
    while i < n and (line[i] == " " or line[i] == "\t"):
        i += 1

    # Must have '='.
    if i >= n or line[i] != "=":
        # Boolean key with no value — not relevant for our use cases.
        return None
    i += 1  # skip '='

    # Skip whitespace after '='.
    while i < n and (line[i] == " " or line[i] == "\t"):
        i += 1

    value = _parse_value(line, i)
    return key, value


def _parse_value(line: str, start: int) -> str:
    """Parse a config value starting at ``start``.

    Handles quoted strings, escape sequences, and inline comments.
    """
    result: list[str] = []
    in_quote = False
    i = start
    n = len(line)

    while i < n:
        ch = line[i]

        # Inline comments outside quotes end the value.
        if not in_quote and (ch == "#" or ch == ";"):
            break

        if ch == '"':
            in_quote = not in_quote
            i += 1
            continue

        if ch == "\\" and i + 1 < n:
            nxt = line[i + 1]
            if in_quote:
                # Inside quotes: recognize escape sequences.
                if nxt == "n":
                    result.append("\n")
                elif nxt == "t":
                    result.append("\t")
                elif nxt == "b":
                    result.append("\b")
                elif nxt == '"':
                    result.append('"')
                elif nxt == "\\":
                    result.append("\\")
                else:
                    # Git silently drops the backslash for unknown escapes.
                    result.append(nxt)
                i += 2
                continue
            # Outside quotes: backslash at end of line = continuation (we don't handle
            # multi-line since we split on \n, but handle \\ and others).
            if nxt == "\\":
                result.append("\\")
                i += 2
                continue
            # Fallthrough — treat backslash literally outside quotes.

        result.append(ch)
        i += 1

    out = "".join(result)
    # Trim trailing whitespace from unquoted portions. Git trims trailing whitespace that
    # isn't inside quotes; for single-line values, trim the result when not ending in a quote.
    if not in_quote:
        out = _trim_trailing_whitespace(out)

    return out


def _trim_trailing_whitespace(s: str) -> str:
    end = len(s)
    while end > 0 and (s[end - 1] == " " or s[end - 1] == "\t"):
        end -= 1
    return s[:end]


def _matches_section_header(
    line: str,
    section_lower: str,
    subsection: str | None,
) -> bool:
    """Whether a config line like ``[remote "origin"]`` matches section/subsection.

    Section matching is case-insensitive; subsection matching is case-sensitive.
    """
    # line starts with '['.
    i = 1
    n = len(line)

    # Read section name.
    while i < n and line[i] != "]" and line[i] != " " and line[i] != "\t" and line[i] != '"':
        i += 1
    found_section = line[1:i].lower()

    if found_section != section_lower:
        return False

    if subsection is None:
        # Simple section: must end with ']'.
        return i < n and line[i] == "]"

    # Skip whitespace before subsection quote.
    while i < n and (line[i] == " " or line[i] == "\t"):
        i += 1

    # Must have opening quote.
    if i >= n or line[i] != '"':
        return False
    i += 1  # skip opening quote

    # Read subsection — case-sensitive, handle \\ and \" escapes.
    found_subsection: list[str] = []
    while i < n and line[i] != '"':
        if line[i] == "\\" and i + 1 < n:
            nxt = line[i + 1]
            if nxt == "\\" or nxt == '"':
                found_subsection.append(nxt)
                i += 2
                continue
            # Git drops the backslash for other escapes in subsections.
            found_subsection.append(nxt)
            i += 2
            continue
        found_subsection.append(line[i])
        i += 1

    # Must have closing quote followed by ']'.
    if i >= n or line[i] != '"':
        return False
    i += 1  # skip closing quote

    if i >= n or line[i] != "]":
        return False

    return "".join(found_subsection) == subsection


def _is_key_char(ch: str) -> bool:
    return (
        ("a" <= ch <= "z")
        or ("A" <= ch <= "Z")
        or ("0" <= ch <= "9")
        or ch == "-"
    )
