"""Tool validation configuration

Most tools need NO configuration — basic permission-rule validation works automatically. Only tools
with special pattern requirements are listed here: file-glob tools, bash-wildcard tools, and a small
table of custom validators (WebSearch / WebFetch).

Casing: Python identifiers snake_case; the TS const ``TOOL_VALIDATION_CONFIG`` keeps its UPPER_CASE
name (lint-exempt per the implementation plan), and its inner keys keep the TS wire names
(``filePatternTools`` / ``bashPrefixTools`` / ``customValidation``) since they are a stable config
shape. A custom validator returns ``{"valid": bool, "error"?: str, "suggestion"?: str,
"examples"?: list[str]}`` — the same keys the TS returns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# A custom validator: takes a permission-rule content string, returns the result dict above.
CustomValidator = Callable[[str], dict[str, Any]]


def _validate_web_search(content: str) -> dict[str, Any]:
    """WebSearch doesn't support wildcards or complex patterns."""
    if "*" in content or "?" in content:
        return {
            "valid": False,
            "error": "WebSearch does not support wildcards",
            "suggestion": "Use exact search terms without * or ?",
            "examples": ["WebSearch(tabvis ai)", "WebSearch(typescript tutorial)"],
        }
    return {"valid": True}


def _validate_web_fetch(content: str) -> dict[str, Any]:
    """WebFetch uses a ``domain:`` prefix for hostname-based permissions."""
    # Check if it's trying to use a URL format.
    if "://" in content or content.startswith("http"):
        return {
            "valid": False,
            "error": "WebFetch permissions use domain format, not URLs",
            "suggestion": 'Use "domain:hostname" format',
            "examples": [
                "WebFetch(domain:example.com)",
                "WebFetch(domain:github.com)",
            ],
        }

    # Must start with a domain: prefix.
    if not content.startswith("domain:"):
        return {
            "valid": False,
            "error": 'WebFetch permissions must use "domain:" prefix',
            "suggestion": 'Use "domain:hostname" format',
            "examples": [
                "WebFetch(domain:example.com)",
                "WebFetch(domain:*.example.com)",
            ],
        }

    # Allow wildcards in domain patterns (valid: domain:*.example.com, domain:example.*, etc.).
    return {"valid": True}


# Wire-name keys map to their snake_case validation bindings.
TOOL_VALIDATION_CONFIG: dict[str, Any] = {
    # File pattern tools (accept *.ts, src/**, etc.).
    "filePatternTools": [
        "Read",
        "Write",
        "Edit",
        "Glob",
        "NotebookRead",
        "NotebookEdit",
    ],
    # Bash wildcard tools (accept * anywhere, and legacy command:* syntax).
    "bashPrefixTools": ["Bash"],
    # Custom validation (only if needed).
    "customValidation": {
        "WebSearch": _validate_web_search,
        "WebFetch": _validate_web_fetch,
    },
}


def is_file_pattern_tool(tool_name: str) -> bool:
    """True if ``tool_name`` accepts file glob patterns."""
    return tool_name in TOOL_VALIDATION_CONFIG["filePatternTools"]


def is_bash_prefix_tool(tool_name: str) -> bool:
    """True if ``tool_name`` accepts bash wildcard / legacy ``:*`` patterns (``isBashPrefixTool``)."""
    return tool_name in TOOL_VALIDATION_CONFIG["bashPrefixTools"]


def get_custom_validation(tool_name: str) -> CustomValidator | None:
    """The custom validator for ``tool_name``, or ``None``."""
    return TOOL_VALIDATION_CONFIG["customValidation"].get(tool_name)
