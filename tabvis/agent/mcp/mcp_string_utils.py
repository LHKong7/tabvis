"""Pure string utilities for MCP tool/server name parsing.

No heavy dependencies, so consumers that only need string parsing (e.g. permission validation) can
import it cheaply. ``normalize_name_for_mcp`` is a dependency-free pure function also exposed by
:mod:`tabvis.agent.tools.mcp_tool`; it is inlined here to keep this module free of the ``Tool`` base
import.

Casing: Python identifiers snake_case; the returned ``mcp_info_from_string`` dict keeps the wire keys
``serverName`` / ``toolName`` (round-trips to the permission system + transcript).
"""

from __future__ import annotations

import re

# Tabvis.ai server names are prefixed with this string.
_REMOTE_SERVER_PREFIX = "Tabvis "

_INVALID_MCP_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_COLLAPSE_UNDERSCORES = re.compile(r"_+")
_TRIM_UNDERSCORES = re.compile(r"^_|_$")


def normalize_name_for_mcp(name: str) -> str:
    """Normalize a server/tool name for use in an MCP tool name.

    Replaces any char outside ``[a-zA-Z0-9_-]`` with ``_`` so the name matches the API pattern
    ``^[a-zA-Z0-9_-]{1,64}$``. For Tabvis servers (names starting with ``"Tabvis "``) it also collapses
    runs of ``_`` and strips leading/trailing ``_`` so they don't interfere with the ``__``
    delimiter used in MCP tool names.
    """
    normalized = _INVALID_MCP_NAME_CHARS.sub("_", name)
    if name.startswith(_REMOTE_SERVER_PREFIX):
        normalized = _COLLAPSE_UNDERSCORES.sub("_", normalized)
        normalized = _TRIM_UNDERSCORES.sub("", normalized)
    return normalized


def mcp_info_from_string(tool_string: str) -> dict[str, str | None] | None:
    """Extract MCP server info from a tool-name string.

    Expected format ``"mcp__serverName__toolName"``. Returns ``{"serverName", "toolName"}`` (toolName
    ``None`` when absent), or ``None`` when the string is not a valid MCP rule.

    Known limitation: a server name containing ``__`` parses incorrectly — e.g.
    ``"mcp__my__server__tool"`` parses as ``server="my"`` / ``tool="server__tool"``.
    """
    parts = tool_string.split("__")
    # Split into the "mcp" marker, the server name, and the remaining tool-name parts.
    mcp_part = parts[0] if len(parts) > 0 else None
    server_name = parts[1] if len(parts) > 1 else None
    tool_name_parts = parts[2:]
    if mcp_part != "mcp" or not server_name:
        return None
    # Join all parts after server name to preserve double underscores in tool names.
    tool_name = "__".join(tool_name_parts) if tool_name_parts else None
    return {"serverName": server_name, "toolName": tool_name}


def get_mcp_prefix(server_name: str) -> str:
    """The ``mcp__<normalized server>__`` prefix for a server."""
    return f"mcp__{normalize_name_for_mcp(server_name)}__"


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    """Fully qualified ``mcp__server__tool`` name (inverse of :func:`mcp_info_from_string`).

    Both names are normalized.
    """
    return f"{get_mcp_prefix(server_name)}{normalize_name_for_mcp(tool_name)}"


def get_tool_name_for_permission_check(tool: dict) -> str:
    """Name to use for permission-rule matching.

    For MCP tools (``tool["mcpInfo"]`` present), uses the fully qualified ``mcp__server__tool`` name
    so deny rules targeting builtins (e.g. ``"Write"``) don't match unprefixed MCP replacements that
    share the same display name. Falls back to ``tool["name"]``.
    """
    mcp_info = tool.get("mcpInfo")
    if mcp_info:
        return build_mcp_tool_name(mcp_info["serverName"], mcp_info["toolName"])
    return tool["name"]


def get_mcp_display_name(full_name: str, server_name: str) -> str:
    """Strip the MCP prefix from a full tool/command name.

    ``full_name`` e.g. ``"mcp__server_name__tool_name"``; ``server_name`` is removed from the prefix.
    """
    prefix = f"mcp__{normalize_name_for_mcp(server_name)}__"
    return full_name.replace(prefix, "")


_MCP_SUFFIX_RE = re.compile(r"\s*\(MCP\)\s*$")


def extract_mcp_tool_display_name(user_facing_name: str) -> str:
    """Extract just the display name from a user-facing name.

    ``user_facing_name`` e.g. ``"github - Add comment to issue (MCP)"`` -> ``"Add comment to issue"``.
    Removes the ``(MCP)`` suffix, then the server prefix (everything before ``" - "``).
    """
    # First, remove the (MCP) suffix if present.
    without_suffix = _MCP_SUFFIX_RE.sub("", user_facing_name)

    # Trim the result.
    without_suffix = without_suffix.strip()

    # Then, remove the server prefix (everything before " - ").
    dash_index = without_suffix.find(" - ")
    if dash_index != -1:
        return without_suffix[dash_index + 3 :].strip()

    # If no dash found, return the string without (MCP).
    return without_suffix
