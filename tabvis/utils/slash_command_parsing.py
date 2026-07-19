"""Slash-command parsing

A single pure helper, ``parseSlashCommand``, that splits a raw ``/command args...`` input into its
``commandName`` / ``args`` / ``isMcp`` parts (or ``None`` when the input is not a slash command).

Casing: the result is a small data record. The TS type ``ParsedSlashCommand`` has the fields
``commandName`` / ``args`` / ``isMcp``. These are *not* JSON wire keys that round-trip to the API
or transcript — they are an internal in-process shape — so per the naming conventions the Python
identifiers become snake_case (``command_name`` / ``args`` / ``is_mcp``). A frozen dataclass is
used rather than a dict so callers get attribute access and a stable shape.

Faithful behavior notes:
- Splits on a single space character (``" "``), matching ``withoutSlash.split(' ')`` — NOT on
  arbitrary whitespace. So runs of spaces produce empty tokens, and ``args`` is the remaining
  tokens re-joined with single spaces (collapsing nothing, preserving the TS ``.join(' ')`` exactly
  including any empty tokens it re-inserts).
- ``input.trim()`` (Python ``str.strip()``) is applied first; an empty / non-``/`` input → ``None``.
- An empty first token (``words[0]`` falsy in TS — e.g. input ``"/ foo"`` → first token ``""``)
  → ``None``.
- MCP detection: when the *second* space-token is exactly ``"(MCP)"``, the command name gets
  ``" (MCP)"`` appended, ``is_mcp`` is ``True``, and args start at token index 2.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedSlashCommand:
    """Parsed components of a slash-command input."""

    command_name: str
    args: str
    is_mcp: bool


def parse_slash_command(input: str) -> ParsedSlashCommand | None:
    """Parse a slash-command input string into its component parts.

    Args:
        input: The raw input string (should start with ``/``).

    Returns:
        A :class:`ParsedSlashCommand` (``command_name`` / ``args`` / ``is_mcp``), or ``None`` when
        the input is not a valid slash command.

    Examples:
        ``parse_slash_command("/search foo bar")`` →
        ``ParsedSlashCommand(command_name="search", args="foo bar", is_mcp=False)``

        ``parse_slash_command("/mcp:tool (MCP) arg1 arg2")`` →
        ``ParsedSlashCommand(command_name="mcp:tool (MCP)", args="arg1 arg2", is_mcp=True)``
    """
    trimmed_input = input.strip()

    # Check if input starts with '/'.
    if not trimmed_input.startswith("/"):
        return None

    # Remove the leading '/' and split by single spaces (parity with JS .split(' ')).
    without_slash = trimmed_input[1:]
    words = without_slash.split(" ")

    # Falsy first token (empty string) → invalid (TS: `if (!words[0])`).
    if not words[0]:
        return None

    command_name = words[0]
    is_mcp = False
    args_start_index = 1

    # MCP commands: the second word is exactly '(MCP)'.
    if len(words) > 1 and words[1] == "(MCP)":
        command_name = command_name + " (MCP)"
        is_mcp = True
        args_start_index = 2

    # Everything after the command name, re-joined with single spaces.
    args = " ".join(words[args_start_index:])

    return ParsedSlashCommand(command_name=command_name, args=args, is_mcp=is_mcp)
