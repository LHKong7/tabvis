"""Shared permission-rule matching for shell tools.

Extracts the common logic for:

- Parsing permission rules (exact, prefix, wildcard).
- Matching commands against wildcard rules.
- Generating permission suggestions.

npm -> PyPI: the JS ``RegExp`` machinery is implemented with the stdlib :mod:`re` module. The JS ``s``
(dotAll) flag maps to :data:`re.DOTALL`; the ``i`` (ignore-case) flag maps to :data:`re.IGNORECASE`.
The two null-byte sentinel placeholders are kept verbatim so the escape/unescape round-trip matches
the TS byte-for-byte.

Casing: Python identifiers are snake_case; suggestion dicts round-trip to settings JSON, so they
keep their wire keys verbatim (``type`` / ``rules`` / ``toolName`` / ``ruleContent`` / ``behavior``
/ ``destination``).
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

__all__ = [
    "ExactRule",
    "PrefixRule",
    "ShellPermissionRule",
    "WildcardRule",
    "has_wildcards",
    "match_wildcard_pattern",
    "parse_permission_rule",
    "permission_rule_extract_prefix",
    "suggestion_for_exact_command",
    "suggestion_for_prefix",
]

# Null-byte sentinel placeholders for wildcard pattern escaping — module-level so the compiled
# regexes are built once instead of per permission check.
_ESCAPED_STAR_PLACEHOLDER = "\x00ESCAPED_STAR\x00"
_ESCAPED_BACKSLASH_PLACEHOLDER = "\x00ESCAPED_BACKSLASH\x00"
_ESCAPED_STAR_PLACEHOLDER_RE = re.compile(re.escape(_ESCAPED_STAR_PLACEHOLDER))
_ESCAPED_BACKSLASH_PLACEHOLDER_RE = re.compile(re.escape(_ESCAPED_BACKSLASH_PLACEHOLDER))

# Legacy ":*" prefix extraction: /^(.+):\*$/
_LEGACY_PREFIX_RE = re.compile(r"^(.+):\*$")

# Regex special characters to escape (everything except '*'), matching the JS char class
# /[.+?^${}()|[\]\\'"]/g.
_REGEX_SPECIAL_RE = re.compile(r"[.+?^${}()|\[\]\\'\"]")


class ExactRule(TypedDict):
    type: Literal["exact"]
    command: str


class PrefixRule(TypedDict):
    type: Literal["prefix"]
    prefix: str


class WildcardRule(TypedDict):
    type: Literal["wildcard"]
    pattern: str


# Parsed permission rule discriminated union.
ShellPermissionRule = ExactRule | PrefixRule | WildcardRule


def permission_rule_extract_prefix(permission_rule: str) -> str | None:
    """Extract the prefix from legacy ``:*`` syntax (e.g. ``"npm:*"`` -> ``"npm"``).

    Maintained for backwards compatibility. Returns ``None`` when the rule is not legacy syntax.
    """
    match = _LEGACY_PREFIX_RE.match(permission_rule)
    return match.group(1) if match else None


def has_wildcards(pattern: str) -> bool:
    """Whether ``pattern`` contains unescaped wildcards (not the legacy ``:*`` syntax).

    Returns ``True`` if the pattern contains ``*`` that are not escaped with ``\\`` or part of a
    trailing ``:*``. An asterisk is unescaped if preceded by an even number of backslashes
    (including zero).
    """
    # If it ends with :*, it's legacy prefix syntax, not wildcard.
    if pattern.endswith(":*"):
        return False
    # Check for an unescaped * anywhere in the pattern.
    for i, ch in enumerate(pattern):
        if ch == "*":
            # Count backslashes before this asterisk.
            backslash_count = 0
            j = i - 1
            while j >= 0 and pattern[j] == "\\":
                backslash_count += 1
                j -= 1
            # If even number of backslashes (including 0), the asterisk is unescaped.
            if backslash_count % 2 == 0:
                return True
    return False


def match_wildcard_pattern(
    pattern: str,
    command: str,
    case_insensitive: bool = False,
) -> bool:
    """Match a command against a wildcard pattern.

    Wildcards (``*``) match any sequence of characters. Use ``\\*`` to match a literal asterisk and
    ``\\\\`` to match a literal backslash.
    """
    # Trim leading/trailing whitespace from the pattern.
    trimmed_pattern = pattern.strip()

    # Process the pattern to handle escape sequences: \* and \\.
    processed_parts: list[str] = []
    i = 0
    length = len(trimmed_pattern)
    while i < length:
        char = trimmed_pattern[i]

        # Handle escape sequences.
        if char == "\\" and i + 1 < length:
            next_char = trimmed_pattern[i + 1]
            if next_char == "*":
                # \* -> literal asterisk placeholder.
                processed_parts.append(_ESCAPED_STAR_PLACEHOLDER)
                i += 2
                continue
            if next_char == "\\":
                # \\ -> literal backslash placeholder.
                processed_parts.append(_ESCAPED_BACKSLASH_PLACEHOLDER)
                i += 2
                continue

        processed_parts.append(char)
        i += 1

    processed = "".join(processed_parts)

    # Escape regex special characters except *.
    escaped = _REGEX_SPECIAL_RE.sub(lambda m: "\\" + m.group(0), processed)

    # Convert unescaped * to .* for wildcard matching.
    with_wildcards = escaped.replace("*", ".*")

    # Convert placeholders back to escaped regex literals.
    regex_pattern = _ESCAPED_STAR_PLACEHOLDER_RE.sub("\\\\*", with_wildcards)
    regex_pattern = _ESCAPED_BACKSLASH_PLACEHOLDER_RE.sub("\\\\\\\\", regex_pattern)

    # When a pattern ends with ' *' (space + unescaped wildcard) AND the trailing wildcard is the
    # ONLY unescaped wildcard, make the trailing space-and-args optional so 'git *' matches both
    # 'git add' and bare 'git'. Multi-wildcard patterns like '* run *' are excluded.
    unescaped_star_count = processed.count("*")
    if regex_pattern.endswith(" .*") and unescaped_star_count == 1:
        regex_pattern = regex_pattern[:-3] + "( .*)?"

    # Create a regex that matches the entire string. The dotAll flag makes '.' match newlines so
    # wildcards match commands containing embedded newlines.
    flags = re.DOTALL
    if case_insensitive:
        flags |= re.IGNORECASE
    regex = re.compile(f"^{regex_pattern}$", flags)

    return regex.match(command) is not None


def parse_permission_rule(permission_rule: str) -> ShellPermissionRule:
    """Parse a permission rule string into a structured rule object."""
    # Check for legacy :* prefix syntax first (backwards compatibility).
    prefix = permission_rule_extract_prefix(permission_rule)
    if prefix is not None:
        return {"type": "prefix", "prefix": prefix}

    # Check for new wildcard syntax (contains * but not :* at end).
    if has_wildcards(permission_rule):
        return {"type": "wildcard", "pattern": permission_rule}

    # Otherwise, it's an exact match.
    return {"type": "exact", "command": permission_rule}


def suggestion_for_exact_command(tool_name: str, command: str) -> list[dict]:
    """Generate a permission-update suggestion for an exact command match.

    The returned dict round-trips to settings JSON, so its keys stay wire-form
    (``type`` / ``rules`` / ``toolName`` / ``ruleContent`` / ``behavior`` / ``destination``).
    """
    return [
        {
            "type": "addRules",
            "rules": [
                {
                    "toolName": tool_name,
                    "ruleContent": command,
                },
            ],
            "behavior": "allow",
            "destination": "localSettings",
        },
    ]


def suggestion_for_prefix(tool_name: str, prefix: str) -> list[dict]:
    """Generate a permission-update suggestion for a prefix match."""
    return [
        {
            "type": "addRules",
            "rules": [
                {
                    "toolName": tool_name,
                    "ruleContent": f"{prefix}:*",
                },
            ],
            "behavior": "allow",
            "destination": "localSettings",
        },
    ]
