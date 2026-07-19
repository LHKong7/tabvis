"""Tool Search utilities for dynamically discovering deferred tools.

When enabled, deferred tools (MCP and
``shouldDefer`` tools) are sent with ``defer_loading: true`` and discovered via
``ToolSearchTool`` rather than loaded upfront. This module decides whether tool
search is on for a given request (``tst`` / ``tst-auto`` / ``standard``), extracts
discovered tool names from ``tool_reference`` blocks, and computes deferred-tool
pool deltas.

CYCLE: part of the ``context-tokens`` cluster. ``countToolDefinitionTokens`` +
``TOOL_TOKEN_COUNT_OVERHEAD`` come from the cycle sibling
:mod:`tabvis.utils.analyze_context`, imported function-locally so this module
imports standalone.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from tabvis.utils.array import count
from tabvis.utils.context import get_context_window_for_model
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_defined_falsy, is_env_truthy
from tabvis.utils.model.providers import (
    get_api_provider,
    is_first_party_provider_base_url)
from tabvis.utils.slow_operations import json_stringify
from tabvis.utils.zod_to_json_schema import zod_to_json_schema

if TYPE_CHECKING:  # type-only — avoid runtime cycle edges
    from tabvis.tool import ToolPermissionContext, Tools

Message = dict[str, Any]
ToolSearchMode = Literal["tst", "tst-auto", "standard"]

#: Default percentage of context window at which to auto-enable tool search.
DEFAULT_AUTO_TOOL_SEARCH_PERCENTAGE = 10  # 10%

#: Approximate chars per token for MCP tool definitions (fallback heuristic).
CHARS_PER_TOKEN = 2.5

#: Default patterns for models that do NOT support tool_reference.
DEFAULT_UNSUPPORTED_MODEL_PATTERNS = ["haiku"]


# --- tool_search_tool bridges (flat-tool module; not a cycle sibling) -----------


def _is_deferred_tool(tool: Any) -> bool:
    from tabvis.agent.tools.tool_search_tool import is_deferred_tool

    return is_deferred_tool(tool)


def _tool_search_tool_name() -> str:
    from tabvis.agent.tools.tool_search_tool import TOOL_SEARCH_TOOL_NAME

    return TOOL_SEARCH_TOOL_NAME


def _format_deferred_tool_line(tool: Any) -> str:
    """Render a deferred tool as a single line, using the tool name."""
    return getattr(tool, "name", "")


def _tool_matches_name(tool: Any, name: str) -> bool:
    from tabvis.tool import tool_matches_name

    return tool_matches_name(tool, name)


# --- auto:N parsing -------------------------------------------------------------


def parse_auto_percentage(value: str) -> int | None:
    """Parse ``auto:N`` from ``ENABLE_TOOL_SEARCH``; clamp to 0-100, else ``None``."""
    if not value.startswith("auto:"):
        return None

    percent_str = value[5:]
    try:
        percent = int(percent_str, 10)
    except ValueError:
        log_for_debugging(
            f'Invalid ENABLE_TOOL_SEARCH value "{value}": expected auto:N '
            "where N is a number."
        )
        return None

    return max(0, min(100, percent))


def is_auto_tool_search_mode(value: str | None) -> bool:
    """True if ``ENABLE_TOOL_SEARCH`` is ``auto`` or ``auto:N``."""
    if not value:
        return False
    return value == "auto" or value.startswith("auto:")


def get_auto_tool_search_percentage() -> int:
    """Auto-enable percentage from env var or default."""
    value = os.environ.get("ENABLE_TOOL_SEARCH")
    if not value:
        return DEFAULT_AUTO_TOOL_SEARCH_PERCENTAGE

    if value == "auto":
        return DEFAULT_AUTO_TOOL_SEARCH_PERCENTAGE

    parsed = parse_auto_percentage(value)
    if parsed is not None:
        return parsed

    return DEFAULT_AUTO_TOOL_SEARCH_PERCENTAGE


def get_auto_tool_search_token_threshold(model: str) -> int:
    """Token threshold for auto-enabling tool search for a model."""
    betas: list[str] = []
    context_window = get_context_window_for_model(model, betas)
    percentage = get_auto_tool_search_percentage() / 100
    return int(context_window * percentage)


def get_auto_tool_search_char_threshold(model: str) -> int:
    """Character threshold for auto-enabling (fallback when token API unavailable)."""
    return int(get_auto_tool_search_token_threshold(model) * CHARS_PER_TOKEN)


# --- memoized deferred-tool token count ----------------------------------------

_deferred_tool_token_cache: dict[str, int | None] = {}


def _deferred_tool_cache_key(tools: Tools) -> str:
    return ",".join(t.name for t in tools if _is_deferred_tool(t))


async def get_deferred_tool_token_count(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agents: list[Any],
    model: str,
) -> int | None:
    """Total token count for all deferred tools (memoized by deferred tool names).

    Returns ``None`` if the token API is unavailable (caller falls back to the char
    heuristic). The memo cache is keyed by deferred tool names, so it invalidates
    when MCP servers connect/disconnect.
    """
    key = _deferred_tool_cache_key(tools)
    if key in _deferred_tool_token_cache:
        return _deferred_tool_token_cache[key]

    result = await _compute_deferred_tool_token_count(
        tools, get_tool_permission_context, agents, model
    )
    _deferred_tool_token_cache[key] = result
    return result


async def _compute_deferred_tool_token_count(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agents: list[Any],
    model: str,
) -> int | None:
    deferred_tools = [t for t in tools if _is_deferred_tool(t)]
    if len(deferred_tools) == 0:
        return 0

    try:
        from tabvis.utils.analyze_context import (
            TOOL_TOKEN_COUNT_OVERHEAD,
            count_tool_definition_tokens)

        total = await count_tool_definition_tokens(
            deferred_tools,
            get_tool_permission_context,
            {"activeAgents": agents, "allAgents": agents},
            model,
        )
        if total == 0:
            return None  # API unavailable
        return max(0, total - TOOL_TOKEN_COUNT_OVERHEAD)
    except Exception:  # noqa: BLE001 - fall back to char heuristic
        return None


def reset_deferred_tool_token_cache() -> None:
    """Clear the memoized deferred-tool token count cache (for tests)."""
    _deferred_tool_token_cache.clear()


# --- mode resolution ------------------------------------------------------------


def get_tool_search_mode() -> ToolSearchMode:
    """Resolve the tool-search mode from ``ENABLE_TOOL_SEARCH``.

    ===================  =========
    ENABLE_TOOL_SEARCH   Mode
    ===================  =========
    auto / auto:1-99     tst-auto
    true / auto:0        tst
    false / auto:100     standard
    (unset)              tst
    ===================  =========
    """
    # TABVIS_DISABLE_EXPERIMENTAL_BETAS is a kill switch for beta API features.
    if is_env_truthy(os.environ.get("TABVIS_DISABLE_EXPERIMENTAL_BETAS")):
        return "standard"

    value = os.environ.get("ENABLE_TOOL_SEARCH")

    # Handle auto:N syntax — check edge cases first.
    auto_percent = parse_auto_percentage(value) if value else None
    if auto_percent == 0:
        return "tst"  # auto:0 = always enabled
    if auto_percent == 100:
        return "standard"
    if is_auto_tool_search_mode(value):
        return "tst-auto"  # auto or auto:1-99

    if is_env_truthy(value):
        return "tst"
    if is_env_defined_falsy(os.environ.get("ENABLE_TOOL_SEARCH")):
        return "standard"
    return "tst"  # default: always defer MCP and shouldDefer tools


def get_unsupported_tool_reference_patterns() -> list[str]:
    """Model patterns that do NOT support tool_reference (GrowthBook-configurable)."""
    try:
        patterns = None
        if patterns and isinstance(patterns, list) and len(patterns) > 0:
            return patterns
    except Exception:  # noqa: BLE001 - GrowthBook not ready, use defaults
        pass
    return DEFAULT_UNSUPPORTED_MODEL_PATTERNS


def model_supports_tool_reference(model: str) -> bool:
    """Negative test: models support tool_reference unless they match an unsupported pattern."""
    normalized_model = model.lower()
    unsupported_patterns = get_unsupported_tool_reference_patterns()

    for pattern in unsupported_patterns:
        if pattern.lower() in normalized_model:
            return False

    return True


_logged_optimistic = False


def isToolSearchEnabledOptimistic() -> bool:  # noqa: N802 - exported camel for parity
    """See :func:`is_tool_search_enabled_optimistic`."""
    return is_tool_search_enabled_optimistic()


def is_tool_search_enabled_optimistic() -> bool:
    """Optimistic check: could tool search potentially be enabled?

    Returns ``False`` only when tool search is definitively disabled (standard mode
    or a non-first-party base URL with default settings). For the definitive check
    use :func:`is_tool_search_enabled`.
    """
    global _logged_optimistic
    mode = get_tool_search_mode()
    if mode == "standard":
        if not _logged_optimistic:
            _logged_optimistic = True
            log_for_debugging(
                f"[ToolSearch:optimistic] mode={mode}, "
                f"ENABLE_TOOL_SEARCH={os.environ.get('ENABLE_TOOL_SEARCH')}, "
                "result=false"
            )
        return False

    # tool_reference is a beta content type that third-party API gateways
    # typically don't support. Only gate when ENABLE_TOOL_SEARCH is unset/empty.
    if (
        not os.environ.get("ENABLE_TOOL_SEARCH")
        and get_api_provider() == "firstParty"
        and not is_first_party_provider_base_url()
    ):
        if not _logged_optimistic:
            _logged_optimistic = True
            log_for_debugging(
                "[ToolSearch:optimistic] disabled: "
                f"TABVIS_BASE_URL={os.environ.get('TABVIS_BASE_URL')} is not a "
                "first-party Provider host. Set ENABLE_TOOL_SEARCH=true (or auto / "
                "auto:N) if your proxy forwards tool_reference blocks."
            )
        return False

    if not _logged_optimistic:
        _logged_optimistic = True
        log_for_debugging(
            f"[ToolSearch:optimistic] mode={mode}, "
            f"ENABLE_TOOL_SEARCH={os.environ.get('ENABLE_TOOL_SEARCH')}, "
            "result=true"
        )
    return True


def is_tool_search_tool_available(tools: list[Any]) -> bool:
    """True if ToolSearchTool is in the tools list (respects disallowedTools)."""
    name = _tool_search_tool_name()
    return any(_tool_matches_name(tool, name) for tool in tools)


async def _calculate_deferred_tool_description_chars(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agents: list[Any],
) -> int:
    """Total deferred tool description size in chars (name + description + schema)."""
    deferred_tools = [t for t in tools if _is_deferred_tool(t)]
    if len(deferred_tools) == 0:
        return 0

    total = 0
    for tool in deferred_tools:
        description = await tool.prompt(
            {
                "getToolPermissionContext": get_tool_permission_context,
                "tools": tools,
                "agents": agents,
            }
        )
        input_json_schema = getattr(tool, "input_json_schema", None) or getattr(
            tool, "inputJSONSchema", None
        )
        input_schema = getattr(tool, "input_schema", None) or getattr(
            tool, "inputSchema", None
        )
        if input_json_schema:
            schema_str = json_stringify(input_json_schema)
        elif input_schema is not None:
            schema_str = json_stringify(zod_to_json_schema(input_schema))
        else:
            schema_str = ""
        total += len(tool.name) + len(description) + len(schema_str)

    return total


async def is_tool_search_enabled(
    model: str,
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agents: list[Any],
    source: str | None = None,
) -> bool:
    """Definitive per-request check for whether tool search is enabled.

    Includes MCP mode, model compatibility (haiku lacks tool_reference),
    ToolSearchTool availability, and the threshold check for ``tst-auto`` mode.
    """
    mcp_tool_count = count(tools, lambda t: t.is_mcp)

    def log_mode_decision(
        enabled: bool,
        mode: ToolSearchMode,
        reason: str,
        extra_props: dict[str, int] | None = None,
    ) -> None:
        props: dict[str, Any] = {
            "enabled": enabled,
            "mode": mode,
            "reason": reason,
            # Log the actual model being checked, not the session's main model.
            "checkedModel": model,
            "mcpToolCount": mcp_tool_count,
            "userType": os.environ.get("USER_TYPE") or "external",
        }
        if extra_props:
            props.update(extra_props)

    # Check if model supports tool_reference.
    if not model_supports_tool_reference(model):
        log_for_debugging(
            f"Tool search disabled for model '{model}': model does not support "
            "tool_reference blocks. This feature is only available on TABVIS "
            "Balanced 4+, TABVIS Max 4+, and newer models."
        )
        log_mode_decision(False, "standard", "model_unsupported")
        return False

    # Check if ToolSearchTool is available (respects disallowedTools).
    if not is_tool_search_tool_available(tools):
        log_for_debugging(
            "Tool search disabled: ToolSearchTool is not available (may have been "
            "disallowed via disallowedTools)."
        )
        log_mode_decision(False, "standard", "mcp_search_unavailable")
        return False

    mode = get_tool_search_mode()

    if mode == "tst":
        log_mode_decision(True, mode, "tst_enabled")
        return True

    if mode == "tst-auto":
        result = await _check_auto_threshold(
            tools, get_tool_permission_context, agents, model
        )
        enabled = result["enabled"]
        debug_description = result["debugDescription"]
        metrics = result["metrics"]

        if enabled:
            log_for_debugging(
                f"Auto tool search enabled: {debug_description}"
                + (f" [source: {source}]" if source else "")
            )
            log_mode_decision(True, mode, "auto_above_threshold", metrics)
            return True

        log_for_debugging(
            f"Auto tool search disabled: {debug_description}"
            + (f" [source: {source}]" if source else "")
        )
        log_mode_decision(False, mode, "auto_below_threshold", metrics)
        return False

    # mode == 'standard'
    log_mode_decision(False, mode, "standard_mode")
    return False


def is_tool_reference_block(obj: Any) -> bool:
    """True if ``obj`` is a ``tool_reference`` block (runtime beta-feature check)."""
    return isinstance(obj, dict) and obj.get("type") == "tool_reference"


def _is_tool_reference_with_name(obj: Any) -> bool:
    """Type guard for a tool_reference block carrying a string ``tool_name``."""
    return (
        is_tool_reference_block(obj)
        and "tool_name" in obj
        and isinstance(obj["tool_name"], str)
    )


def _is_tool_result_block_with_content(obj: Any) -> bool:
    """Type guard for tool_result blocks with array content."""
    return (
        isinstance(obj, dict)
        and obj.get("type") == "tool_result"
        and isinstance(obj.get("content"), list)
    )


def extract_discovered_tool_names(messages: list[Message]) -> set[str]:
    """Tool names discovered via ``tool_reference`` blocks across message history.

    Compaction snapshots the discovered set onto
    ``compactMetadata.preCompactDiscoveredTools`` on the boundary marker; this scan
    reads it back. (Inline type checks rather than ``is_compact_boundary_message``
    to avoid the messages.py ↔ tool_search.py cycle.)
    """
    discovered_tools: set[str] = set()
    carried_from_boundary = 0

    for msg in messages:
        if msg.get("type") == "system" and msg.get("subtype") == "compact_boundary":
            compact_metadata = msg.get("compactMetadata") or {}
            carried = compact_metadata.get("preCompactDiscoveredTools")
            if carried:
                for name in carried:
                    discovered_tools.add(name)
                carried_from_boundary += len(carried)
            continue

        # Only user messages contain tool_result blocks.
        if msg.get("type") != "user":
            continue

        content = msg.get("message", {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if _is_tool_result_block_with_content(block):
                for item in block["content"]:
                    if _is_tool_reference_with_name(item):
                        discovered_tools.add(item["tool_name"])

    if len(discovered_tools) > 0:
        log_for_debugging(
            f"Dynamic tool loading: found {len(discovered_tools)} discovered tools "
            "in message history"
            + (
                f" ({carried_from_boundary} carried from compact boundary)"
                if carried_from_boundary > 0
                else ""
            )
        )

    return discovered_tools


@dataclass
class DeferredToolsDelta:
    """Delta of the deferred-tool pool versus what was already announced."""

    added_names: list[str] = field(default_factory=list)
    #: Rendered lines for added_names; the scan reconstructs from names.
    added_lines: list[str] = field(default_factory=list)
    removed_names: list[str] = field(default_factory=list)


@dataclass
class DeferredToolsDeltaScanContext:
    """Call-site discriminator for the ``tengu_deferred_tools_pool_change`` event."""

    call_site: Literal[
        "attachments_main",
        "attachments_subagent",
        "compact_full",
        "compact_partial",
        "reactive_compact",
    ]
    query_source: str | None = None


def is_deferred_tools_delta_enabled() -> bool:
    """True → announce deferred tools via persisted delta attachments."""
    return False


def get_deferred_tools_delta(
    tools: Tools,
    messages: list[Message],
    scan_context: DeferredToolsDeltaScanContext | None = None,
) -> DeferredToolsDelta | None:
    """Diff the current deferred-tool pool against what's already been announced.

    Returns ``None`` if nothing changed. A name that was announced but has since
    stopped being deferred — yet is still in the base pool — is NOT reported as
    removed (it's now loaded directly).
    """
    announced: set[str] = set()
    attachment_count = 0
    dtd_count = 0
    attachment_types_seen: set[str] = set()
    for msg in messages:
        if msg.get("type") != "attachment":
            continue
        attachment_count += 1
        attachment = msg.get("attachment") or {}
        attachment_types_seen.add(attachment.get("type"))
        if attachment.get("type") != "deferred_tools_delta":
            continue
        dtd_count += 1
        for n in attachment.get("addedNames", []):
            announced.add(n)
        for n in attachment.get("removedNames", []):
            announced.discard(n)

    deferred = [t for t in tools if _is_deferred_tool(t)]
    deferred_names = {t.name for t in deferred}
    pool_names = {t.name for t in tools}

    added = [t for t in deferred if t.name not in announced]
    removed: list[str] = []
    for n in announced:
        if n in deferred_names:
            continue
        if n not in pool_names:
            removed.append(n)
        # else: undeferred — silent

    if len(added) == 0 and len(removed) == 0:
        return None

    return DeferredToolsDelta(
        added_names=sorted(t.name for t in added),
        added_lines=sorted(_format_deferred_tool_line(t) for t in added),
        removed_names=sorted(removed),
    )


async def _check_auto_threshold(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agents: list[Any],
    model: str,
) -> dict[str, Any]:
    """Check whether deferred tools exceed the auto-threshold for enabling TST.

    Tries exact token count first; falls back to a character-based heuristic.
    """
    deferred_tool_tokens = await get_deferred_tool_token_count(
        tools, get_tool_permission_context, agents, model
    )

    if deferred_tool_tokens is not None:
        threshold = get_auto_tool_search_token_threshold(model)
        return {
            "enabled": deferred_tool_tokens >= threshold,
            "debugDescription": (
                f"{deferred_tool_tokens} tokens (threshold: {threshold}, "
                f"{get_auto_tool_search_percentage()}% of context)"
            ),
            "metrics": {
                "deferredToolTokens": deferred_tool_tokens,
                "threshold": threshold,
            },
        }

    # Fallback: character-based heuristic when token API is unavailable.
    deferred_tool_description_chars = await _calculate_deferred_tool_description_chars(
        tools, get_tool_permission_context, agents
    )
    char_threshold = get_auto_tool_search_char_threshold(model)
    return {
        "enabled": deferred_tool_description_chars >= char_threshold,
        "debugDescription": (
            f"{deferred_tool_description_chars} chars (threshold: {char_threshold}, "
            f"{get_auto_tool_search_percentage()}% of context) (char fallback)"
        ),
        "metrics": {
            "deferredToolDescriptionChars": deferred_tool_description_chars,
            "charThreshold": char_threshold,
        },
    }
