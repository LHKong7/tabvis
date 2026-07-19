"""Permission-rule string grammar.

Parses and serialises the ``"ToolName"`` / ``"ToolName(content)"`` permission-rule grammar,
escaping parentheses inside content so they survive the round-trip, and normalises legacy tool
names to their canonical form.

Flat tools architecture: the TS tool-name constants (``AGENT_TOOL_NAME`` from
``tools/AgentTool/constants.ts``, ``TASK_OUTPUT_TOOL_NAME`` from ``tools/TaskOutputTool/
constants.ts``, ``TASK_STOP_TOOL_NAME`` from ``tools/TaskStopTool/prompt.ts``) resolve to the
flat ``tabvis/tools/<tool>.py`` symbols (``tabvis.agent.tools.agent_tool`` / ``tabvis.agent.tools.task_output_tool``
/ ``tabvis.agent.tools.task_stop_tool``) — never a ``tabvis/tools/<tool>/`` package.

Casing: Python identifiers are snake_case; the parser returns / consumes
:data:`PermissionRuleValue` dicts, which keep their camelCase wire keys (``toolName`` /
``ruleContent``) because those round-trip to settings / transcript JSON verbatim.
"""

from __future__ import annotations

from tabvis.agent.tools.agent_tool import AGENT_TOOL_NAME
from tabvis.constants.tools import TASK_OUTPUT_TOOL_NAME, TASK_STOP_TOOL_NAME
from tabvis.types.permissions import PermissionRuleValue

__all__ = [
    "escape_rule_content",
    "get_legacy_tool_names",
    "normalize_legacy_tool_name",
    "permission_rule_value_from_string",
    "permission_rule_value_to_string",
    "unescape_rule_content",
]


# Maps legacy tool names to their current canonical names.
# When a tool is renamed, add old -> new here so permission rules, hooks, and persisted wire
# names resolve to the canonical name.
_LEGACY_TOOL_NAME_ALIASES: dict[str, str] = {
    "Task": AGENT_TOOL_NAME,
    "KillShell": TASK_STOP_TOOL_NAME,
    "AgentOutputTool": TASK_OUTPUT_TOOL_NAME,
    "BashOutputTool": TASK_OUTPUT_TOOL_NAME,
}


def normalize_legacy_tool_name(name: str) -> str:
    """Map a legacy tool name to its canonical form (or return ``name`` unchanged)."""
    return _LEGACY_TOOL_NAME_ALIASES.get(name, name)


def get_legacy_tool_names(canonical_name: str) -> list[str]:
    """Return every legacy alias that resolves to ``canonical_name``."""
    result: list[str] = []
    for legacy, canonical in _LEGACY_TOOL_NAME_ALIASES.items():
        if canonical == canonical_name:
            result.append(legacy)
    return result


def escape_rule_content(content: str) -> str:
    """Escape special characters in rule content for safe storage in permission rules.

    Permission rules use the format ``"Tool(content)"``, so parentheses in content must be
    escaped. Escaping order matters: backslashes first (``\\`` -> ``\\\\``), then parentheses
    (``(`` -> ``\\(``, ``)`` -> ``\\)``).

    Example::

        escape_rule_content('psycopg2.connect()')  # => 'psycopg2.connect\\(\\)'
    """
    return (
        content.replace("\\", "\\\\")  # Escape backslashes first
        .replace("(", "\\(")  # Escape opening parentheses
        .replace(")", "\\)")  # Escape closing parentheses
    )


def unescape_rule_content(content: str) -> str:
    """Reverse :func:`escape_rule_content` after parsing from a permission rule.

    Unescaping order matters (reverse of escaping): parentheses first (``\\(`` -> ``(``,
    ``\\)`` -> ``)``), then backslashes (``\\\\`` -> ``\\``).
    """
    return (
        content.replace("\\(", "(")  # Unescape opening parentheses
        .replace("\\)", ")")  # Unescape closing parentheses
        .replace("\\\\", "\\")  # Unescape backslashes last
    )


def _find_first_unescaped_char(string: str, char: str) -> int:
    """Index of the first unescaped ``char``, or ``-1``.

    A character is escaped when preceded by an odd number of backslashes.
    """
    for i, ch in enumerate(string):
        if ch == char:
            backslash_count = 0
            j = i - 1
            while j >= 0 and string[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return i
    return -1


def _find_last_unescaped_char(string: str, char: str) -> int:
    """Index of the last unescaped ``char``, or ``-1``.

    A character is escaped when preceded by an odd number of backslashes.
    """
    for i in range(len(string) - 1, -1, -1):
        if string[i] == char:
            backslash_count = 0
            j = i - 1
            while j >= 0 and string[j] == "\\":
                backslash_count += 1
                j -= 1
            if backslash_count % 2 == 0:
                return i
    return -1


def permission_rule_value_from_string(rule_string: str) -> PermissionRuleValue:
    """Parse a permission-rule string into its components.

    Format: ``"ToolName"`` or ``"ToolName(content)"``. Content may contain escaped parentheses
    (``\\(`` and ``\\)``). The first *unescaped* ``(`` and the last *unescaped* ``)`` delimit the
    content; if there is no such pair, or text trails the closing paren, or the tool name is
    empty, the whole string is treated as a (legacy-normalised) tool name.

    Empty content (``"Bash()"``) or a standalone wildcard (``"Bash(*)"``) collapse to the
    tool-wide rule (just the tool name).
    """
    # Find the first unescaped opening parenthesis.
    open_paren_index = _find_first_unescaped_char(rule_string, "(")
    if open_paren_index == -1:
        # No parenthesis found - this is just a tool name.
        return {"toolName": normalize_legacy_tool_name(rule_string)}

    # Find the last unescaped closing parenthesis.
    close_paren_index = _find_last_unescaped_char(rule_string, ")")
    if close_paren_index == -1 or close_paren_index <= open_paren_index:
        # No matching closing paren or malformed - treat as tool name.
        return {"toolName": normalize_legacy_tool_name(rule_string)}

    # Ensure the closing paren is at the end.
    if close_paren_index != len(rule_string) - 1:
        # Content after closing paren - treat as tool name.
        return {"toolName": normalize_legacy_tool_name(rule_string)}

    tool_name = rule_string[:open_paren_index]
    raw_content = rule_string[open_paren_index + 1 : close_paren_index]

    # Missing toolName (e.g., "(foo)") is malformed - treat whole string as tool name.
    if not tool_name:
        return {"toolName": normalize_legacy_tool_name(rule_string)}

    # Empty content (e.g., "Bash()") or standalone wildcard (e.g., "Bash(*)") should be treated
    # as just the tool name (tool-wide rule).
    if raw_content == "" or raw_content == "*":
        return {"toolName": normalize_legacy_tool_name(tool_name)}

    # Unescape the content.
    rule_content = unescape_rule_content(raw_content)
    return {"toolName": normalize_legacy_tool_name(tool_name), "ruleContent": rule_content}


def permission_rule_value_to_string(rule_value: PermissionRuleValue) -> str:
    """Serialise a :data:`PermissionRuleValue` back to its string form.

    Escapes parentheses in the content to prevent parsing issues. A missing / empty
    ``ruleContent`` collapses to the bare tool name.
    """
    if not rule_value.get("ruleContent"):
        return rule_value["toolName"]
    escaped_content = escape_rule_content(rule_value["ruleContent"])
    return f"{rule_value['toolName']}({escaped_content})"
