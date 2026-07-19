"""Frontmatter parser for markdown files

Extracts and parses YAML frontmatter between ``---`` delimiters, plus the small family of
frontmatter-value coercers (``split_path_in_frontmatter`` brace expansion,
``parse_positive_int_from_frontmatter``, ``coerce_description_to_string``,
``parse_boolean_frontmatter``, ``parse_shell_frontmatter``).

Casing: Python identifiers are snake_case. ``FrontmatterData`` is a plain ``dict`` (a parsed
YAML mapping) — its keys are wire data (e.g. ``allowed-tools``/``argument-hint``/
``hide-from-slash-command-tool`` keep their kebab-case exactly as authored in the .md file),
so the parsed dict's keys are preserved verbatim. Only Python identifiers (function names)
change.

Faithful-behavior notes:
- ``FRONTMATTER_REGEX`` mirrors the TS ``^---\\s*\\n([\\s\\S]*?)---\\s*\\n?`` and is compiled
  with ``re.DOTALL`` so ``.`` spans newlines (the JS ``[\\s\\S]`` idiom).
- ``parse_frontmatter`` first tries ``parse_yaml`` directly; on failure it retries after
  ``_quote_problematic_values`` (so glob patterns like ``**/*.{ts,tsx}`` parse). A second
  failure logs via ``log_for_debugging`` (warn) and returns ``{}`` frontmatter.
- A parsed value that is not an object (scalar / list) is rejected (``frontmatter`` stays
  ``{}``) — matching the TS ``typeof parsed === 'object' && !Array.isArray(parsed)`` guard.
- ``coerce_description_to_string`` coerces numbers/bools via ``str``; ``True``/``False`` render
  as ``"True"``/``"False"`` (Python) where TS renders ``"true"``/``"false"`` — but YAML booleans
  in frontmatter parse to Python ``bool`` and the JS ``String(true)`` path is for genuine
  booleans only; the lone realistic frontmatter case (a description) is a string, so this edge
  is not exercised in practice. Documented for fidelity.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.settings.types import HooksSettings  # noqa: F401 - re-exported for parity/typing
from tabvis.utils.yaml import parse_yaml

# ``FrontmatterData`` is a parsed YAML mapping. In TS it is a structural type with a documented
# set of optional kebab/snake keys plus an open ``[key: string]: unknown`` index signature. In
# Python the parsed value is simply a ``dict`` whose keys are preserved verbatim (wire data).
FrontmatterData = dict[str, Any]


# ``ParsedMarkdown`` is ``{frontmatter, content}``. Represented as a plain dict to keep the
# round-trip shape; the wire-key field names ``frontmatter``/``content`` are preserved.
ParsedMarkdown = dict[str, Any]


# Characters that require quoting in YAML values (when unquoted) — see the TS comment block:
# - { } flow mapping indicators
# - * anchor/alias indicator
# - [ ] flow sequence indicators
# - ': ' (colon followed by space) key indicator — matched as the pattern (not bare ':') so
#   '12:34' times and 'https://' URLs stay unquoted
# - # comment indicator, & anchor, ! tag, | > block scalars, % directive, @ ` reserved
YAML_SPECIAL_CHARS = re.compile(r"[{}\[\]*&#!|>%@`]|: ")

# A simple ``key: value`` line (not indented, not a list item, not a block scalar).
_SIMPLE_KV_RE = re.compile(r"^([a-zA-Z_-]+):\s+(.+)$")

# Frontmatter fence: ``---\n ... ---`` (the inner group is the frontmatter text). ``re.DOTALL``
# makes ``.`` span newlines (parity with the JS ``[\s\S]`` character class). Non-greedy ``*?``.
FRONTMATTER_REGEX = re.compile(r"^---\s*\n(.*?)---\s*\n?", re.DOTALL)

# TS ``expandBraces``: /^([^{]*)\{([^}]+)\}(.*)$/ — no s-flag, so the trailing (.*) does NOT span
# newlines. Glob patterns never contain newlines, but keep the no-DOTALL parity for fidelity.
_BRACE_GROUP_RE = re.compile(r"^([^{]*)\{([^}]+)\}(.*)$")


def _quote_problematic_values(frontmatter_text: str) -> str:
    """Quote values that contain special YAML characters.

    Allows glob patterns like ``**/*.{ts,tsx}`` to be parsed correctly.
    ``quoteProblematicValues``.
    """
    lines = frontmatter_text.split("\n")
    result: list[str] = []

    for line in lines:
        match = _SIMPLE_KV_RE.match(line)
        if match:
            key = match.group(1)
            value = match.group(2)
            if not key or not value:
                result.append(line)
                continue

            # Skip if already quoted.
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                result.append(line)
                continue

            # Quote if it contains special YAML characters.
            if YAML_SPECIAL_CHARS.search(value):
                # Use double quotes and escape any existing backslashes / double quotes.
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                result.append(f'{key}: "{escaped}"')
                continue

        result.append(line)

    return "\n".join(result)


def parse_frontmatter(
    markdown: str,
    source_path: str | None = None,
) -> ParsedMarkdown:
    """Parse markdown content to extract frontmatter and content.

    Returns ``{"frontmatter": dict, "content": str}``. With no
    frontmatter fence, returns ``{"frontmatter": {}, "content": markdown}``.
    """
    match = FRONTMATTER_REGEX.match(markdown)

    if not match:
        # No frontmatter found.
        return {
            "frontmatter": {},
            "content": markdown,
        }

    frontmatter_text = match.group(1) or ""
    content = markdown[match.end() :]

    frontmatter: FrontmatterData = {}
    try:
        parsed = parse_yaml(frontmatter_text)
        if parsed is not None and isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:  # noqa: BLE001 - YAML parse failure; retry after quoting
        # YAML parsing failed — try again after quoting problematic values.
        try:
            quoted_text = _quote_problematic_values(frontmatter_text)
            parsed = parse_yaml(quoted_text)
            if parsed is not None and isinstance(parsed, dict):
                frontmatter = parsed
        except Exception as retry_error:  # noqa: BLE001
            # Still failed — log for debugging so users can diagnose broken frontmatter.
            location = f" in {source_path}" if source_path else ""
            log_for_debugging(
                f"Failed to parse YAML frontmatter{location}: {retry_error}",
                {"level": "warn"},
            )

    return {
        "frontmatter": frontmatter,
        "content": content,
    }


def split_path_in_frontmatter(input: str | list[str]) -> list[str]:
    """Split a comma-separated string and expand brace patterns.

    Commas inside braces are not treated as separators. Also accepts a YAML list (string array)
    for ergonomic frontmatter.

    Examples:
        ``split_path_in_frontmatter("a, b")`` → ``["a", "b"]``
        ``split_path_in_frontmatter("a, src/*.{ts,tsx}")`` → ``["a", "src/*.ts", "src/*.tsx"]``
        ``split_path_in_frontmatter("{a,b}/{c,d}")`` → ``["a/c", "a/d", "b/c", "b/d"]``
    """
    if isinstance(input, list):
        out: list[str] = []
        for item in input:
            out.extend(split_path_in_frontmatter(item))
        return out
    if not isinstance(input, str):
        return []

    # Split by comma while respecting braces.
    parts: list[str] = []
    current = ""
    brace_depth = 0

    for char in input:
        if char == "{":
            brace_depth += 1
            current += char
        elif char == "}":
            brace_depth -= 1
            current += char
        elif char == "," and brace_depth == 0:
            # Split here — a comma outside of braces.
            trimmed = current.strip()
            if trimmed:
                parts.append(trimmed)
            current = ""
        else:
            current += char

    # Add the last part.
    trimmed = current.strip()
    if trimmed:
        parts.append(trimmed)

    # Expand brace patterns in each (non-empty) part.
    expanded: list[str] = []
    for pattern in parts:
        if len(pattern) > 0:
            expanded.extend(_expand_braces(pattern))
    return expanded


def _expand_braces(pattern: str) -> list[str]:
    """Expand brace patterns in a glob string.

    Examples:
        ``_expand_braces("src/*.{ts,tsx}")`` → ``["src/*.ts", "src/*.tsx"]``
        ``_expand_braces("{a,b}/{c,d}")`` → ``["a/c", "a/d", "b/c", "b/d"]``
    """
    brace_match = _BRACE_GROUP_RE.match(pattern)

    if not brace_match:
        # No braces found, return pattern as-is.
        return [pattern]

    prefix = brace_match.group(1) or ""
    alternatives = brace_match.group(2) or ""
    suffix = brace_match.group(3) or ""

    # Split alternatives by comma and expand each one.
    alt_parts = [alt.strip() for alt in alternatives.split(",")]

    expanded: list[str] = []
    for part in alt_parts:
        combined = prefix + part + suffix
        # Recursively handle additional brace groups.
        expanded.extend(_expand_braces(combined))

    return expanded


def parse_positive_int_from_frontmatter(value: Any) -> int | None:
    """Parse a positive integer value from frontmatter (number or string repr).

    Returns the parsed positive integer, or
    ``None`` if invalid or not provided.
    """
    if value is None:
        return None

    # JS: ``typeof value === 'number' ? value : parseInt(String(value), 10)``. A Python ``bool``
    # is an ``int`` subclass; JS would treat ``true`` as a non-number → ``parseInt("true")`` →
    # NaN → None. Mirror that by excluding bool from the numeric fast path.
    if isinstance(value, int) and not isinstance(value, bool):
        parsed: float | int = value
    elif isinstance(value, float):
        parsed = value
    else:
        parsed = _parse_int(str(value))

    # ``Number.isInteger(parsed) && parsed > 0``.
    if parsed is not None and float(parsed).is_integer() and parsed > 0:
        return int(parsed)

    return None


def _parse_int(text: str) -> float | None:
    """Leading integer, ignoring trailing garbage.

    Returns ``None`` for no leading integer (JS ``NaN``).
    """
    stripped = text.lstrip()
    match = re.match(r"[+-]?\d+", stripped)
    if not match:
        return None
    return int(match.group(0))


def coerce_description_to_string(
    value: Any,
    component_name: str | None = None,
    source_name: str | None = None,
) -> str | None:
    """Validate and coerce a description value from frontmatter.
    ``coerceDescriptionToString``.

    Strings are returned trimmed (empty/whitespace-only → ``None``). Numbers / booleans are
    coerced via ``str``. Non-scalar values (lists, dicts) are invalid → logged then omitted
    (``None``). ``None`` → ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    # ``typeof value === 'number' || typeof value === 'boolean'``. Bool is checked first since a
    # Python ``bool`` is an ``int`` subclass.
    if isinstance(value, (bool, int, float)):
        return str(value)
    # Non-scalar descriptions (lists, dicts) are invalid — log and omit.
    source = (
        f"{source_name}:{component_name}"
        if source_name
        else (component_name if component_name is not None else "unknown")
    )
    log_for_debugging(
        f"Description invalid for {source} - omitting",
        {"level": "warn"},
    )
    return None


def parse_boolean_frontmatter(value: Any) -> bool:
    """Parse a boolean frontmatter value.

    Only returns ``True`` for literal ``True`` or the ``"true"`` string.
    """
    return value is True or value == "true"


# Shell values accepted in ``shell:`` frontmatter for .md ``!``-block execution.
FrontmatterShell = Literal["bash", "powershell"]

FRONTMATTER_SHELLS: tuple[str, ...] = ("bash", "powershell")


def parse_shell_frontmatter(value: Any, source: str) -> FrontmatterShell | None:
    """Parse and validate the ``shell:`` frontmatter field.

    Returns ``None`` for absent/null/empty (caller defaults to bash). Logs a warning and
    returns ``None`` for unrecognized values — falling back to bash rather than failing the
    skill load.
    """
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized == "":
        return None
    if normalized in FRONTMATTER_SHELLS:
        return normalized  # type: ignore[return-value]
    log_for_debugging(
        f"Frontmatter 'shell: {value}' in {source} is not recognized. "
        f"Valid values: {', '.join(FRONTMATTER_SHELLS)}. Falling back to bash.",
        {"level": "warn"},
    )
    return None
