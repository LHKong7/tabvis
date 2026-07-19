"""Permission-rule format/content validation

Validates a single permission-rule string (e.g. ``"Bash(npm run build)"``, ``"Edit(src/**)"``,
``"mcp__server__tool"``) for the settings file ``permissions.{allow,deny,ask}`` arrays. Checks
parenthesis balancing (escape-aware), empty parens, MCP-rule shape, tool-name casing, and the
per-tool content rules (bash ``:*`` legacy prefix, file-glob placement) sourced from
:mod:`tabvis.utils.settings.tool_validation_config`.

Casing: Python identifiers snake_case; the returned result dict keeps the TS wire keys
(``valid`` / ``error`` / ``suggestion`` / ``examples``). The exported ``PermissionRuleSchema`` is a
pydantic ``Annotated[str, ...]`` validator — the
camelCase rule-source semantics are unchanged.

The TS ``lazySchema`` wrapper is replaced by an eagerly-built pydantic ``TypeAdapter`` (the schema
has no forward refs); zod's ``ZodIssueCode.custom`` + ``params.received`` becomes a pydantic
``ValueError`` whose message mirrors the TS-composed string.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import AfterValidator, TypeAdapter

from tabvis.agent.mcp.mcp_string_utils import mcp_info_from_string

from ..permissions.permission_rule_parser import permission_rule_value_from_string
from ..string_utils import capitalize
from .tool_validation_config import (
    get_custom_validation,
    is_bash_prefix_tool,
    is_file_pattern_tool,
)


def _is_escaped(string: str, index: int) -> bool:
    """True if ``string[index]`` is preceded by an odd number of backslashes."""
    backslash_count = 0
    j = index - 1
    while j >= 0 and string[j] == "\\":
        backslash_count += 1
        j -= 1
    return backslash_count % 2 != 0


def _count_unescaped_char(string: str, char: str) -> int:
    """Count unescaped occurrences of ``char`` in ``string``."""
    count = 0
    for i in range(len(string)):
        if string[i] == char and not _is_escaped(string, i):
            count += 1
    return count


def _has_unescaped_empty_parens(string: str) -> bool:
    """True if ``string`` contains unescaped adjacent ``()``."""
    for i in range(len(string) - 1):
        if string[i] == "(" and string[i + 1] == ")":
            # Check if the opening paren is unescaped.
            if not _is_escaped(string, i):
                return True
    return False


def validate_permission_rule(rule: str) -> dict[str, Any]:
    """Validate a permission-rule string.

    Returns ``{"valid": bool, "error"?: str, "suggestion"?: str, "examples"?: list[str]}`` — the
    same keys the TS returns (only ``valid`` is always present).
    """
    # Empty rule check.
    if not rule or rule.strip() == "":
        return {"valid": False, "error": "Permission rule cannot be empty"}

    # Check parentheses matching first (only count unescaped parens).
    open_count = _count_unescaped_char(rule, "(")
    close_count = _count_unescaped_char(rule, ")")
    if open_count != close_count:
        return {
            "valid": False,
            "error": "Mismatched parentheses",
            "suggestion": "Ensure all opening parentheses have matching closing parentheses",
        }

    # Check for empty parentheses (escape-aware).
    if _has_unescaped_empty_parens(rule):
        tool_name = rule[: rule.index("(")]
        if not tool_name:
            return {
                "valid": False,
                "error": "Empty parentheses with no tool name",
                "suggestion": "Specify a tool name before the parentheses",
            }
        return {
            "valid": False,
            "error": "Empty parentheses",
            "suggestion": f'Either specify a pattern or use just "{tool_name}" without parentheses',
            "examples": [f"{tool_name}", f"{tool_name}(some-pattern)"],
        }

    # Parse the rule.
    parsed = permission_rule_value_from_string(rule)
    parsed_tool_name = parsed.get("toolName")
    rule_content = parsed.get("ruleContent")

    # MCP validation — must be done before general tool validation.
    mcp_info = mcp_info_from_string(parsed_tool_name) if parsed_tool_name else None
    if mcp_info:
        # MCP rules support server-level, tool-level, and wildcard permissions.
        # Valid formats:
        # - mcp__server (server-level, all tools)
        # - mcp__server__* (wildcard, all tools - equivalent to server-level)
        # - mcp__server__tool (specific tool)
        #
        # MCP rules cannot have any pattern/content (parentheses). Check both parsed content and
        # raw string since the parser normalizes standalone wildcards (e.g. "mcp__server(*)") to
        # undefined ruleContent.
        if rule_content is not None or _count_unescaped_char(rule, "(") > 0:
            server_name = mcp_info["serverName"]
            tool_name = mcp_info.get("toolName")
            examples = [
                f"mcp__{server_name}",
                f"mcp__{server_name}__*",
                (
                    f"mcp__{server_name}__{tool_name}"
                    if tool_name and tool_name != "*"
                    else None
                ),
            ]
            return {
                "valid": False,
                "error": "MCP rules do not support patterns in parentheses",
                "suggestion": (
                    f'Use "{parsed_tool_name}" without parentheses, or use '
                    f'"mcp__{server_name}__*" for all tools'
                ),
                "examples": [e for e in examples if e],
            }

        return {"valid": True}  # Valid MCP rule.

    # Tool name validation (for non-MCP tools).
    if not parsed_tool_name or len(parsed_tool_name) == 0:
        return {"valid": False, "error": "Tool name cannot be empty"}

    # Check tool name starts with uppercase (standard tools).
    if parsed_tool_name[0] != parsed_tool_name[0].upper():
        return {
            "valid": False,
            "error": "Tool names must start with uppercase",
            "suggestion": f'Use "{capitalize(str(parsed_tool_name))}"',
        }

    # Check for custom validation rules first.
    custom_validation = get_custom_validation(parsed_tool_name)
    if custom_validation and rule_content is not None:
        custom_result = custom_validation(rule_content)
        if not custom_result["valid"]:
            return custom_result

    # Bash-specific validation.
    if is_bash_prefix_tool(parsed_tool_name) and rule_content is not None:
        content = rule_content

        # Check for common :* mistakes — :* must be at the end (legacy prefix syntax).
        if ":*" in content and not content.endswith(":*"):
            return {
                "valid": False,
                "error": "The :* pattern must be at the end",
                "suggestion": (
                    "Move :* to the end for prefix matching, or use * for wildcard matching"
                ),
                "examples": [
                    "Bash(npm run:*) - prefix matching (legacy)",
                    "Bash(npm run *) - wildcard matching",
                ],
            }

        # Check for :* without a prefix.
        if content == ":*":
            return {
                "valid": False,
                "error": "Prefix cannot be empty before :*",
                "suggestion": "Specify a command prefix before :*",
                "examples": ["Bash(npm:*)", "Bash(git:*)"],
            }

        # Note: We don't validate quote balancing because bash quoting rules are complex.
        # A command like `grep '"'` has valid unbalanced double quotes. Users who create
        # patterns with unintended quote mismatches will discover the issue when matching
        # doesn't work as expected.
        #
        # Wildcards are now allowed at any position for flexible pattern matching. Legacy :*
        # syntax continues to work for backwards compatibility.

    # File tool validation.
    if is_file_pattern_tool(parsed_tool_name) and rule_content is not None:
        content = rule_content

        # Check for :* in file patterns (common mistake from Bash patterns).
        if ":*" in content:
            return {
                "valid": False,
                "error": 'The ":*" syntax is only for Bash prefix rules',
                "suggestion": 'Use glob patterns like "*" or "**" for file matching',
                "examples": [
                    f"{parsed_tool_name}(*.ts) - matches .ts files",
                    f"{parsed_tool_name}(src/**) - matches all files in src",
                    f"{parsed_tool_name}(**/*.test.ts) - matches test files",
                ],
            }

        # Warn about wildcards not at boundaries. The TS regex is
        # ``/^\*|\*$|\*\*|\/\*|\*\.|\*\)/`` (boundary patterns); this is a loose check —
        # wildcards in the middle might be valid in some cases but often indicate confusion.
        if "*" in content and not _matches_wildcard_boundary(content) and "**" not in content:
            return {
                "valid": False,
                "error": "Wildcard placement might be incorrect",
                "suggestion": "Wildcards are typically used at path boundaries",
                "examples": [
                    f"{parsed_tool_name}(*.js) - all .js files",
                    f"{parsed_tool_name}(src/*) - all files directly in src",
                    f"{parsed_tool_name}(src/**) - all files recursively in src",
                ],
            }

    return {"valid": True}


# Wildcard boundary syntax: leading/trailing ``*``, ``**``, ``/*``, ``*.``, or ``*)``.
_WILDCARD_BOUNDARY_RE = re.compile(r"^\*|\*$|\*\*|/\*|\*\.|\*\)")


def _matches_wildcard_boundary(content: str) -> bool:
    """True if a wildcard sits at a recognized path boundary."""
    return _WILDCARD_BOUNDARY_RE.search(content) is not None


def _validate_permission_rule_str(value: str) -> str:
    """Pydantic ``AfterValidator`` for a permission-rule string.

    Raises ``ValueError`` with the TS-composed message (``error`` + ``. suggestion`` +
    ``. Examples: ...``) when the rule is invalid; returns the value unchanged when valid.
    """
    result = validate_permission_rule(value)
    if not result["valid"]:
        message = result["error"]
        suggestion = result.get("suggestion")
        if suggestion:
            message += f". {suggestion}"
        examples = result.get("examples")
        if examples:
            message += f". Examples: {', '.join(examples)}"
        raise ValueError(message)
    return value


# Custom validator type for permission-rule array entries.
PermissionRule = Annotated[str, AfterValidator(_validate_permission_rule_str)]

# Eager TypeAdapter (the zod ``lazySchema`` has no forward refs, so no laziness is required).
PermissionRuleSchema: TypeAdapter[str] = TypeAdapter(PermissionRule)
