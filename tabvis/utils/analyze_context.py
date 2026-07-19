"""Context-window analysis for the ``/context`` view.

:func:`analyze_context_usage` is the heavy orchestrator: it counts tokens for the
system prompt (including project instructions and auto-memory), tools (built-in / MCP / deferred), agents, slash
commands, skills, and messages, then lays out a grid of context categories for
rendering. :func:`count_tool_definition_tokens` and ``TOOL_TOKEN_COUNT_OVERHEAD``
are also consumed by :mod:`tabvis.utils.tool_search`.

CYCLE: part of the ``context-tokens`` cluster. Every cross-cycle reference
(``token_estimation``, ``tool_search``, ``tokens``, ``micro_compact``, ``auto_compact``,
``tool_search_tool``, ``system_prompt``, skill prompt) is broken with
``TYPE_CHECKING`` type-only imports + function-local lazy runtime imports, so this
module imports standalone even before its siblings exist on disk.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from tabvis.utils.slow_operations import json_stringify

if TYPE_CHECKING:  # type-only — no runtime cycle edge
    from tabvis.tool import ToolPermissionContext, Tools

Message = dict[str, Any]

RESERVED_CATEGORY_NAME = "Autocompact buffer"
MANUAL_COMPACT_BUFFER_NAME = "Compact buffer"

#: Fixed token overhead the API adds once per call when tools are present (~500).
#: When tools are counted individually we subtract this to show accurate sizes.
TOOL_TOKEN_COUNT_OVERHEAD = 500

# Fallback buffer constants — see ``compact/autoCompact.ts`` (cycle sibling).
_AUTOCOMPACT_BUFFER_TOKENS_FALLBACK = 13_000
_MANUAL_COMPACT_BUFFER_TOKENS_FALLBACK = 3_000


def _js_round(value: float) -> int:
    """JS ``Math.round``: round half toward +Infinity (non-negative inputs)."""
    return math.floor(value + 0.5)


# --- cycle-sibling lazy bridges -------------------------------------------------


def _rough_token_count_estimation(content: str) -> int:
    from tabvis.services.token_estimation import rough_token_count_estimation

    return rough_token_count_estimation(content)


async def _count_messages_tokens_with_api(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> int | None:
    from tabvis.services.token_estimation import count_messages_tokens_with_api

    return await count_messages_tokens_with_api(messages, tools)


async def _count_tokens_via_haiku_fallback(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> int | None:
    from tabvis.services.token_estimation import count_tokens_via_haiku_fallback

    return await count_tokens_via_haiku_fallback(messages, tools)


def _normalize_messages_for_api(messages: list[Message]) -> list[Message]:
    from tabvis.utils.messages import normalize_messages_for_api

    return normalize_messages_for_api(messages)


# --- token counting with fallback ----------------------------------------------


async def count_tokens_with_fallback(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> int | None:
    """Count tokens via the API, falling back to the Haiku 1-token request."""
    from tabvis.utils.errors import error_message
    from tabvis.utils.log import log_error

    try:
        result = await _count_messages_tokens_with_api(messages, tools)
        if result is not None:
            return result
        _log_for_debugging(
            f"countTokensWithFallback: API returned null, trying haiku fallback "
            f"({len(tools)} tools)"
        )
    except Exception as err:  # noqa: BLE001 - match TS catch
        _log_for_debugging(f"countTokensWithFallback: API failed: {error_message(err)}")
        log_error(err)

    try:
        fallback_result = await _count_tokens_via_haiku_fallback(messages, tools)
        if fallback_result is None:
            _log_for_debugging(
                f"countTokensWithFallback: haiku fallback also returned null "
                f"({len(tools)} tools)"
            )
        return fallback_result
    except Exception as err:  # noqa: BLE001 - match TS catch
        _log_for_debugging(
            f"countTokensWithFallback: haiku fallback failed: {error_message(err)}"
        )
        log_error(err)
        return None


def _log_for_debugging(*args: Any) -> None:
    from tabvis.utils.debug import log_for_debugging

    log_for_debugging(*args)


async def _tool_to_api_schema(tool: Any, context: dict[str, Any]) -> dict[str, Any]:
    from tabvis.utils.api import tool_to_api_schema

    return await tool_to_api_schema(tool, context)


async def count_tool_definition_tokens(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agent_info: dict[str, Any] | None,
    model: str | None = None,
) -> int:
    """Count tokens for a set of tool schemas (the API adds one overhead per call)."""
    active_agents = (agent_info.get("activeAgents") if agent_info else None) or []
    tool_schemas: list[dict[str, Any]] = []
    for tool in tools:
        tool_schemas.append(
            await _tool_to_api_schema(
                tool,
                {
                    "getToolPermissionContext": get_tool_permission_context,
                    "tools": tools,
                    "agents": active_agents,
                    "model": model,
                },
            )
        )
    result = await count_tokens_with_fallback([], tool_schemas)
    if result is None or result == 0:
        tool_names = ", ".join(t.name for t in tools)
        preview = tool_names[:100] + ("..." if len(tool_names) > 100 else "")
        _log_for_debugging(
            f"countToolDefinitionTokens returned {result} for {len(tools)} tools: "
            f"{preview}"
        )
    return result or 0


def _extract_section_name(content: str) -> str:
    """Extract a human-readable name from a system-prompt section's content."""
    heading_match = re.search(r"^#+\s+(.+)$", content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()
    first_line = ""
    for line in content.split("\n"):
        if len(line.strip()) > 0:
            first_line = line
            break
    return first_line[:40] + "…" if len(first_line) > 40 else first_line


async def _count_system_tokens(
    effective_system_prompt: list[str],
) -> dict[str, Any]:
    """Count tokens for each non-empty system-prompt section + system context."""
    from tabvis.constants.prompts import SYSTEM_PROMPT_DYNAMIC_BOUNDARY

    system_context = await _get_system_context()

    named_entries: list[dict[str, str]] = [
        {"name": _extract_section_name(content), "content": content}
        for content in effective_system_prompt
        if len(content) > 0 and content != SYSTEM_PROMPT_DYNAMIC_BOUNDARY
    ]
    named_entries.extend(
        {"name": name, "content": content}
        for name, content in system_context.items()
        if len(content) > 0
    )

    if len(named_entries) < 1:
        return {"systemPromptTokens": 0, "systemPromptSections": []}

    system_token_counts: list[int | None] = []
    for entry in named_entries:
        system_token_counts.append(
            await count_tokens_with_fallback(
                [{"role": "user", "content": entry["content"]}], []
            )
        )

    system_prompt_sections = [
        {"name": entry["name"], "tokens": system_token_counts[i] or 0}
        for i, entry in enumerate(named_entries)
    ]

    system_prompt_tokens = sum(tokens or 0 for tokens in system_token_counts)

    return {
        "systemPromptTokens": system_prompt_tokens,
        "systemPromptSections": system_prompt_sections,
    }


async def _get_system_context() -> dict[str, str]:
    from tabvis.agent.context import get_system_context

    return await get_system_context()


async def _count_memory_file_tokens() -> dict[str, Any]:
    """Compatibility fields for separately reported instruction-file tokens.

    ``TABVIS.md`` and auto-memory are embedded in the system prompt and already included by the
    system-token count, so reporting either separately here would double-count it.
    """
    return {"memoryFileDetails": [], "tabvisMdTokens": 0}


def _is_tool_search_enabled():  # pragma: no cover - bridge
    from tabvis.utils.tool_search import is_tool_search_enabled

    return is_tool_search_enabled


def _is_deferred_tool(tool: Any) -> bool:
    from tabvis.agent.tools.tool_search_tool import is_deferred_tool

    return is_deferred_tool(tool)


def _tool_matches_name(tool: Any, name: str) -> bool:
    from tabvis.tool import tool_matches_name

    return tool_matches_name(tool, name)


def _find_tool_by_name(tools: Tools, name: str) -> Any:
    from tabvis.tool import find_tool_by_name

    return find_tool_by_name(tools, name)


def _skill_tool_name() -> str:
    from tabvis.agent.tools.skill_tool import SKILL_TOOL_NAME

    return SKILL_TOOL_NAME


async def count_built_in_tool_tokens(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agent_info: dict[str, Any] | None,
    model: str | None = None,
    messages: list[Message] | None = None,
) -> dict[str, Any]:
    """Count built-in (non-MCP) tool tokens, splitting always-loaded vs deferred."""
    built_in_tools = [t for t in tools if not t.is_mcp]
    if len(built_in_tools) < 1:
        return {
            "builtInToolTokens": 0,
            "deferredBuiltinDetails": [],
            "deferredBuiltinTokens": 0,
            "systemToolDetails": [],
        }

    is_tool_search_enabled = _is_tool_search_enabled()
    active_agents = (agent_info.get("activeAgents") if agent_info else None) or []
    is_deferred = await is_tool_search_enabled(
        model or "",
        tools,
        get_tool_permission_context,
        active_agents,
        "analyzeBuiltIn",
    )

    always_loaded_tools = [t for t in built_in_tools if not _is_deferred_tool(t)]
    deferred_builtin_tools = [t for t in built_in_tools if _is_deferred_tool(t)]

    always_loaded_tokens = (
        await count_tool_definition_tokens(
            always_loaded_tools, get_tool_permission_context, agent_info, model
        )
        if len(always_loaded_tools) > 0
        else 0
    )

    # Per-tool breakdown for always-loaded tools (ant-only, proportional split).
    system_tool_details: list[dict[str, Any]] = []

    deferred_builtin_details: list[dict[str, Any]] = []
    loaded_deferred_tokens = 0
    total_deferred_tokens = 0

    if len(deferred_builtin_tools) > 0 and is_deferred:
        loaded_tool_names: set[str] = set()
        if messages:
            deferred_tool_name_set = {t.name for t in deferred_builtin_tools}
            for msg in messages:
                if msg.get("type") == "assistant":
                    for block in msg.get("message", {}).get("content", []):
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and isinstance(block.get("name"), str)
                            and block["name"] in deferred_tool_name_set
                        ):
                            loaded_tool_names.add(block["name"])

        tokens_by_tool = []
        for t in deferred_builtin_tools:
            tokens_by_tool.append(
                await count_tool_definition_tokens(
                    [t], get_tool_permission_context, agent_info, model
                )
            )

        for i, tool in enumerate(deferred_builtin_tools):
            tokens = max(0, (tokens_by_tool[i] or 0) - TOOL_TOKEN_COUNT_OVERHEAD)
            is_loaded = tool.name in loaded_tool_names
            deferred_builtin_details.append(
                {"name": tool.name, "tokens": tokens, "isLoaded": is_loaded}
            )
            total_deferred_tokens += tokens
            if is_loaded:
                loaded_deferred_tokens += tokens
    elif len(deferred_builtin_tools) > 0:
        # Tool search not enabled — count deferred tools as regular.
        deferred_tokens = await count_tool_definition_tokens(
            deferred_builtin_tools, get_tool_permission_context, agent_info, model
        )
        return {
            "builtInToolTokens": always_loaded_tokens + deferred_tokens,
            "deferredBuiltinDetails": [],
            "deferredBuiltinTokens": 0,
            "systemToolDetails": system_tool_details,
        }

    return {
        "builtInToolTokens": always_loaded_tokens + loaded_deferred_tokens,
        "deferredBuiltinDetails": deferred_builtin_details,
        "deferredBuiltinTokens": total_deferred_tokens - loaded_deferred_tokens,
        "systemToolDetails": system_tool_details,
    }


def _find_skill_tool(tools: Tools) -> Any:
    return _find_tool_by_name(tools, _skill_tool_name())


async def _count_slash_command_tokens(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agent_info: dict[str, Any] | None,
) -> dict[str, Any]:
    from tabvis.utils.cwd import get_cwd

    info = await _get_slash_command_info(get_cwd())

    slash_command_tool = _find_skill_tool(tools)
    if not slash_command_tool:
        return {
            "slashCommandTokens": 0,
            "commandInfo": {"totalCommands": 0, "includedCommands": 0},
        }

    slash_command_tokens = await count_tool_definition_tokens(
        [slash_command_tool], get_tool_permission_context, agent_info
    )

    return {
        "slashCommandTokens": slash_command_tokens,
        "commandInfo": {
            "totalCommands": info["totalCommands"],
            "includedCommands": info["includedCommands"],
        },
    }


async def _get_slash_command_info(cwd: str) -> dict[str, int]:
    """Look up slash-command info from the skill tool; zeros if unavailable.

    ``get_skill_tool_info`` / ``get_slash_command_info`` are resolved dynamically on
    ``tabvis.agent.tools.skill_tool``; when neither is present this returns zeros.
    """
    try:
        from tabvis.agent.tools import skill_tool as st

        fn = getattr(st, "get_skill_tool_info", None) or getattr(
            st, "get_slash_command_info", None
        )
        if fn is not None:
            return await fn(cwd)
    except (ImportError, AttributeError):
        pass
    return {"totalCommands": 0, "includedCommands": 0}


async def _count_skill_tokens(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agent_info: dict[str, Any] | None,
) -> dict[str, Any]:
    from tabvis.utils.cwd import get_cwd
    from tabvis.utils.errors import to_error
    from tabvis.utils.log import log_error

    try:
        skills = await _get_limited_skill_tool_commands(get_cwd())

        slash_command_tool = _find_skill_tool(tools)
        if not slash_command_tool:
            return {
                "skillTokens": 0,
                "skillInfo": {
                    "totalSkills": 0,
                    "includedSkills": 0,
                    "skillFrontmatter": [],
                },
            }

        # Counts the entire SlashCommandTool (commands AND skills); tracked
        # separately for display. NOT added to context categories (avoid double-
        # counting with countSlashCommandTokens()).
        skill_tokens = await count_tool_definition_tokens(
            [slash_command_tool], get_tool_permission_context, agent_info
        )

        from tabvis.types.command import get_command_name

        skill_frontmatter = [
            {
                "name": get_command_name(skill),
                "source": (
                    getattr(skill, "source", None)
                    if getattr(skill, "type", None) == "prompt"
                    else "builtin"
                ),
                "tokens": _estimate_skill_frontmatter_tokens(skill),
            }
            for skill in skills
        ]

        return {
            "skillTokens": skill_tokens,
            "skillInfo": {
                "totalSkills": len(skills),
                "includedSkills": len(skills),
                "skillFrontmatter": skill_frontmatter,
            },
        }
    except Exception as error:  # noqa: BLE001 - isolate skill failures
        log_error(to_error(error))
        return {
            "skillTokens": 0,
            "skillInfo": {
                "totalSkills": 0,
                "includedSkills": 0,
                "skillFrontmatter": [],
            },
        }


async def _get_limited_skill_tool_commands(cwd: str) -> list[Any]:
    """Resolve ``get_limited_skill_tool_commands`` dynamically; ``[]`` if unavailable."""
    try:
        from tabvis.agent.tools import skill_tool as st

        fn = getattr(st, "get_limited_skill_tool_commands", None)
        if fn is not None:
            return await fn(cwd)
    except (ImportError, AttributeError):
        pass
    return []


def _estimate_skill_frontmatter_tokens(skill: Any) -> int:
    """Resolve ``estimate_skill_frontmatter_tokens`` dynamically; 0 if unavailable."""
    try:
        from tabvis.agent.skills import load_skills_dir as lsd

        fn = getattr(lsd, "estimate_skill_frontmatter_tokens", None)
        if fn is not None:
            return fn(skill)
    except (ImportError, AttributeError):
        pass
    return 0


async def count_mcp_tool_tokens(
    tools: Tools,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    agent_info: dict[str, Any] | None,
    model: str,
    messages: list[Message] | None = None,
) -> dict[str, Any]:
    """Count MCP tool tokens with a per-tool proportional split for display."""
    mcp_tools = [t for t in tools if t.is_mcp]
    mcp_tool_details: list[dict[str, Any]] = []
    active_agents = (agent_info.get("activeAgents") if agent_info else None) or []

    total_tokens_raw = await count_tool_definition_tokens(
        mcp_tools, get_tool_permission_context, agent_info, model
    )
    total_tokens = max(0, (total_tokens_raw or 0) - TOOL_TOKEN_COUNT_OVERHEAD)

    estimates: list[int] = []
    for t in mcp_tools:
        description = await t.prompt(
            {
                "getToolPermissionContext": get_tool_permission_context,
                "tools": tools,
                "agents": active_agents,
            }
        )
        estimates.append(
            _rough_token_count_estimation(
                json_stringify(
                    {
                        "name": t.name,
                        "description": description,
                        "input_schema": getattr(t, "input_json_schema", None) or {},
                    }
                )
            )
        )
    estimate_total = sum(estimates) or 1
    mcp_tool_tokens_by_tool = [
        _js_round((e / estimate_total) * total_tokens) for e in estimates
    ]

    is_tool_search_enabled = _is_tool_search_enabled()
    is_deferred = await is_tool_search_enabled(
        model, tools, get_tool_permission_context, active_agents, "analyzeMcp"
    )

    loaded_mcp_tool_names: set[str] = set()
    if is_deferred and messages:
        mcp_tool_name_set = {t.name for t in mcp_tools}
        for msg in messages:
            if msg.get("type") == "assistant":
                for block in msg.get("message", {}).get("content", []):
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and isinstance(block.get("name"), str)
                        and block["name"] in mcp_tool_name_set
                    ):
                        loaded_mcp_tool_names.add(block["name"])

    for i, tool in enumerate(mcp_tools):
        parts = tool.name.split("__")
        mcp_tool_details.append(
            {
                "name": tool.name,
                "serverName": parts[1] if len(parts) > 1 else "unknown",
                "tokens": mcp_tool_tokens_by_tool[i],
                "isLoaded": tool.name in loaded_mcp_tool_names
                or not _is_deferred_tool(tool),
            }
        )

    loaded_tokens = 0
    deferred_tokens = 0
    for detail in mcp_tool_details:
        if detail["isLoaded"]:
            loaded_tokens += detail["tokens"]
        elif is_deferred:
            deferred_tokens += detail["tokens"]

    return {
        "mcpToolTokens": loaded_tokens if is_deferred else total_tokens,
        "mcpToolDetails": mcp_tool_details,
        "deferredToolTokens": deferred_tokens,
        "loadedMcpToolNames": loaded_mcp_tool_names,
    }


async def _count_custom_agent_tokens(agent_definitions: dict[str, Any]) -> dict[str, Any]:
    """Count tokens for non-built-in agent definitions."""
    custom_agents = [
        a
        for a in agent_definitions.get("activeAgents", [])
        if getattr(a, "source", None) != "built-in"
    ]
    agent_details: list[dict[str, Any]] = []
    agent_tokens = 0

    token_counts = []
    for agent in custom_agents:
        token_counts.append(
            await count_tokens_with_fallback(
                [
                    {
                        "role": "user",
                        "content": " ".join(
                            [
                                getattr(agent, "agent_type", "")
                                or getattr(agent, "agentType", ""),
                                getattr(agent, "when_to_use", "")
                                or getattr(agent, "whenToUse", ""),
                            ]
                        ),
                    }
                ],
                [],
            )
        )

    for i, agent in enumerate(custom_agents):
        tokens = token_counts[i] or 0
        agent_tokens += tokens or 0
        agent_details.append(
            {
                "agentType": getattr(agent, "agent_type", None)
                or getattr(agent, "agentType", None),
                "source": getattr(agent, "source", None),
                "tokens": tokens or 0,
            }
        )
    return {"agentTokens": agent_tokens, "agentDetails": agent_details}


def _process_assistant_message(msg: Message, breakdown: dict[str, Any]) -> None:
    for block in msg.get("message", {}).get("content", []):
        block_str = json_stringify(block)
        block_tokens = _rough_token_count_estimation(block_str)

        if isinstance(block, dict) and block.get("type") == "tool_use":
            breakdown["toolCallTokens"] += block_tokens
            tool_name = block.get("name") or "unknown"
            breakdown["toolCallsByType"][tool_name] = (
                breakdown["toolCallsByType"].get(tool_name, 0) + block_tokens
            )
        else:
            breakdown["assistantMessageTokens"] += block_tokens


def _process_user_message(
    msg: Message, breakdown: dict[str, Any], tool_use_id_to_name: dict[str, str]
) -> None:
    content = msg.get("message", {}).get("content")
    if isinstance(content, str):
        tokens = _rough_token_count_estimation(content)
        breakdown["userMessageTokens"] += tokens
        return

    for block in content or []:
        block_str = json_stringify(block)
        block_tokens = _rough_token_count_estimation(block_str)

        if isinstance(block, dict) and block.get("type") == "tool_result":
            breakdown["toolResultTokens"] += block_tokens
            tool_use_id = block.get("tool_use_id")
            tool_name = (
                tool_use_id_to_name.get(tool_use_id) if tool_use_id else None
            ) or "unknown"
            breakdown["toolResultsByType"][tool_name] = (
                breakdown["toolResultsByType"].get(tool_name, 0) + block_tokens
            )
        else:
            breakdown["userMessageTokens"] += block_tokens


def _process_attachment(msg: Message, breakdown: dict[str, Any]) -> None:
    attachment = msg.get("attachment") or {}
    content_str = json_stringify(attachment)
    tokens = _rough_token_count_estimation(content_str)
    breakdown["attachmentTokens"] += tokens
    attach_type = attachment.get("type") or "unknown"
    breakdown["attachmentsByType"][attach_type] = (
        breakdown["attachmentsByType"].get(attach_type, 0) + tokens
    )


async def _approximate_message_tokens(messages: list[Message]) -> dict[str, Any]:
    """Per-message-category breakdown after microcompaction, total via the API."""
    from tabvis.agent.compact.micro_compact import microcompact_messages

    microcompact_result = await microcompact_messages(messages)
    result_messages = microcompact_result.get("messages", messages)

    breakdown: dict[str, Any] = {
        "totalTokens": 0,
        "toolCallTokens": 0,
        "toolResultTokens": 0,
        "attachmentTokens": 0,
        "assistantMessageTokens": 0,
        "userMessageTokens": 0,
        "toolCallsByType": {},
        "toolResultsByType": {},
        "attachmentsByType": {},
    }

    tool_use_id_to_name: dict[str, str] = {}
    for msg in result_messages:
        if msg.get("type") == "assistant":
            for block in msg.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_id = block.get("id")
                    tool_name = block.get("name") or "unknown"
                    if tool_use_id:
                        tool_use_id_to_name[tool_use_id] = tool_name

    for msg in result_messages:
        if msg.get("type") == "assistant":
            _process_assistant_message(msg, breakdown)
        elif msg.get("type") == "user":
            _process_user_message(msg, breakdown, tool_use_id_to_name)
        elif msg.get("type") == "attachment":
            _process_attachment(msg, breakdown)

    def _to_count_param(m: Message) -> dict[str, Any]:
        if m.get("type") == "assistant":
            # Strip id etc. — the counting API errors if they're present.
            return {"role": "assistant", "content": m.get("message", {}).get("content")}
        return m.get("message", {})

    approximate = await count_tokens_with_fallback(
        [_to_count_param(m) for m in _normalize_messages_for_api(result_messages)],
        [],
    )

    breakdown["totalTokens"] = approximate or 0
    return breakdown


def _get_auto_compact_constants() -> tuple[int, int, Callable[[str], int], Callable[[], bool]]:
    """Resolve auto-compact buffer constants + helpers, with conservative defaults.

    When ``tabvis.agent.compact.auto_compact`` or a given attribute is unavailable, fall
    back to the buffer constants above and conservative defaults (autocompact disabled).
    """
    try:
        from tabvis.agent.compact import auto_compact as ac

        return (
            getattr(ac, "AUTOCOMPACT_BUFFER_TOKENS", _AUTOCOMPACT_BUFFER_TOKENS_FALLBACK),
            getattr(
                ac, "MANUAL_COMPACT_BUFFER_TOKENS", _MANUAL_COMPACT_BUFFER_TOKENS_FALLBACK
            ),
            getattr(ac, "get_effective_context_window_size", lambda _m: 0),
            getattr(ac, "is_auto_compact_enabled", lambda: False),
        )
    except (ImportError, AttributeError):
        return (
            _AUTOCOMPACT_BUFFER_TOKENS_FALLBACK,
            _MANUAL_COMPACT_BUFFER_TOKENS_FALLBACK,
            lambda _m: 0,
            lambda: False,
        )


async def analyze_context_usage(
    messages: list[Message],
    model: str,
    get_tool_permission_context: Callable[[], Awaitable[ToolPermissionContext]],
    tools: Tools,
    agent_definitions: dict[str, Any],
    terminal_width: int | None = None,
    tool_use_context: Any | None = None,
    main_thread_agent_definition: Any | None = None,
    original_messages: list[Message] | None = None,
) -> dict[str, Any]:
    """Compute the full ``/context`` breakdown: categories, grid, usage, model."""
    from tabvis.bootstrap.state import get_sdk_betas
    from tabvis.constants.prompts import get_system_prompt
    from tabvis.utils.context import get_context_window_for_model
    from tabvis.utils.model.model import get_runtime_main_loop_model
    from tabvis.utils.system_prompt import build_effective_system_prompt
    from tabvis.utils.tokens import get_current_usage

    runtime_model = get_runtime_main_loop_model(
        {
            "permissionMode": (await get_tool_permission_context()).get("mode")
            if isinstance(await get_tool_permission_context(), dict)
            else getattr(await get_tool_permission_context(), "mode", None),
            "mainLoopModel": model,
        }
    )
    context_window = get_context_window_for_model(runtime_model, get_sdk_betas())

    default_system_prompt = await get_system_prompt(tools, runtime_model)
    options = getattr(tool_use_context, "options", None) if tool_use_context else None
    effective_system_prompt = build_effective_system_prompt(
        {
            "mainThreadAgentDefinition": main_thread_agent_definition,
            "toolUseContext": tool_use_context or {"options": {}},
            "customSystemPrompt": getattr(options, "custom_system_prompt", None)
            if options
            else None,
            "defaultSystemPrompt": default_system_prompt,
            "appendSystemPrompt": getattr(options, "append_system_prompt", None)
            if options
            else None,
        }
    )

    (
        system_result,
        memory_result,
        builtin_result,
        mcp_result,
        agent_result,
        slash_result,
        message_breakdown,
    ) = (
        await _count_system_tokens(effective_system_prompt),
        await _count_memory_file_tokens(),
        await count_built_in_tool_tokens(
            tools, get_tool_permission_context, agent_definitions, runtime_model, messages
        ),
        await count_mcp_tool_tokens(
            tools, get_tool_permission_context, agent_definitions, runtime_model, messages
        ),
        await _count_custom_agent_tokens(agent_definitions),
        await _count_slash_command_tokens(
            tools, get_tool_permission_context, agent_definitions
        ),
        await _approximate_message_tokens(messages),
    )

    system_prompt_tokens = system_result["systemPromptTokens"]
    system_result["systemPromptSections"]
    tabvis_md_tokens = memory_result["tabvisMdTokens"]
    memory_file_details = memory_result["memoryFileDetails"]
    built_in_tool_tokens = builtin_result["builtInToolTokens"]
    builtin_result["deferredBuiltinDetails"]
    deferred_builtin_tokens = builtin_result["deferredBuiltinTokens"]
    builtin_result["systemToolDetails"]
    mcp_tool_tokens = mcp_result["mcpToolTokens"]
    mcp_tool_details = mcp_result["mcpToolDetails"]
    deferred_tool_tokens = mcp_result["deferredToolTokens"]
    agent_tokens = agent_result["agentTokens"]
    agent_details = agent_result["agentDetails"]
    slash_command_tokens = slash_result["slashCommandTokens"]
    command_info = slash_result["commandInfo"]

    skill_result = await _count_skill_tokens(
        tools, get_tool_permission_context, agent_definitions
    )
    skill_info = skill_result["skillInfo"]
    skill_frontmatter_tokens = sum(
        skill["tokens"] for skill in skill_info["skillFrontmatter"]
    )

    message_tokens = message_breakdown["totalTokens"]

    (
        autocompact_buffer_tokens,
        manual_compact_buffer_tokens,
        get_effective_context_window_size,
        is_auto_compact_enabled,
    ) = _get_auto_compact_constants()

    is_auto_compact = is_auto_compact_enabled()
    auto_compact_threshold = (
        get_effective_context_window_size(model) - autocompact_buffer_tokens
        if is_auto_compact
        else None
    )

    cats: list[dict[str, Any]] = []

    if system_prompt_tokens > 0:
        cats.append(
            {
                "name": "System prompt",
                "tokens": system_prompt_tokens,
                "color": "promptBorder",
            }
        )

    system_tools_tokens = built_in_tool_tokens - skill_frontmatter_tokens
    if system_tools_tokens > 0:
        cats.append(
            {
                "name": "System tools",
                "tokens": system_tools_tokens,
                "color": "inactive",
            }
        )

    if mcp_tool_tokens > 0:
        cats.append(
            {
                "name": "MCP tools",
                "tokens": mcp_tool_tokens,
                "color": "cyan_FOR_SUBAGENTS_ONLY",
            }
        )

    if deferred_tool_tokens > 0:
        cats.append(
            {
                "name": "MCP tools (deferred)",
                "tokens": deferred_tool_tokens,
                "color": "inactive",
                "isDeferred": True,
            }
        )

    if deferred_builtin_tokens > 0:
        cats.append(
            {
                "name": "System tools (deferred)",
                "tokens": deferred_builtin_tokens,
                "color": "inactive",
                "isDeferred": True,
            }
        )

    if agent_tokens > 0:
        cats.append(
            {"name": "Custom agents", "tokens": agent_tokens, "color": "permission"}
        )

    if tabvis_md_tokens > 0:
        cats.append({"name": "Memory files", "tokens": tabvis_md_tokens, "color": "tabvis"})

    if skill_frontmatter_tokens > 0:
        cats.append(
            {"name": "Skills", "tokens": skill_frontmatter_tokens, "color": "warning"}
        )

    if message_tokens is not None and message_tokens > 0:
        cats.append(
            {
                "name": "Messages",
                "tokens": message_tokens,
                "color": "purple_FOR_SUBAGENTS_ONLY",
            }
        )

    actual_usage = sum(0 if cat.get("isDeferred") else cat["tokens"] for cat in cats)

    reserved_tokens = 0
    # Reactive-only mode (cobalt_raccoon) buffer skip is dead-gated (`if false`).
    if is_auto_compact and auto_compact_threshold is not None:
        reserved_tokens = context_window - auto_compact_threshold
        cats.append(
            {
                "name": RESERVED_CATEGORY_NAME,
                "tokens": reserved_tokens,
                "color": "inactive",
            }
        )
    elif not is_auto_compact:
        reserved_tokens = manual_compact_buffer_tokens
        cats.append(
            {
                "name": MANUAL_COMPACT_BUFFER_NAME,
                "tokens": reserved_tokens,
                "color": "inactive",
            }
        )

    free_tokens = max(0, context_window - actual_usage - reserved_tokens)
    cats.append({"name": "Free space", "tokens": free_tokens, "color": "promptBorder"})

    total_including_reserved = actual_usage

    api_usage = get_current_usage(original_messages or messages)
    total_from_api = (
        api_usage["input_tokens"]
        + api_usage["cache_creation_input_tokens"]
        + api_usage["cache_read_input_tokens"]
        if api_usage
        else None
    )
    final_total_tokens = (
        total_from_api if total_from_api is not None else total_including_reserved
    )

    grid_rows = _build_grid(cats, context_window, terminal_width)

    formatted_message_breakdown = _format_message_breakdown(message_breakdown)

    return {
        "categories": cats,
        "totalTokens": final_total_tokens,
        "maxTokens": context_window,
        "rawMaxTokens": context_window,
        "percentage": _js_round((final_total_tokens / context_window) * 100),
        "gridRows": grid_rows,
        "model": runtime_model,
        "memoryFiles": memory_file_details,
        "mcpTools": mcp_tool_details,
        "deferredBuiltinTools": None,
        "systemTools": None,
        "systemPromptSections": None,
        "agents": agent_details,
        "slashCommands": {
            "totalCommands": command_info["totalCommands"],
            "includedCommands": command_info["includedCommands"],
            "tokens": slash_command_tokens,
        }
        if slash_command_tokens > 0
        else None,
        "skills": {
            "totalSkills": skill_info["totalSkills"],
            "includedSkills": skill_info["includedSkills"],
            "tokens": skill_frontmatter_tokens,
            "skillFrontmatter": skill_info["skillFrontmatter"],
        }
        if skill_frontmatter_tokens > 0
        else None,
        "autoCompactThreshold": auto_compact_threshold,
        "isAutoCompactEnabled": is_auto_compact,
        "messageBreakdown": formatted_message_breakdown,
        "apiUsage": api_usage,
    }


def _build_grid(
    cats: list[dict[str, Any]],
    context_window: int,
    terminal_width: int | None,
) -> list[list[dict[str, Any]]]:
    """Lay out the context grid as rows of squares with per-square metadata."""
    is_narrow_screen = bool(terminal_width and terminal_width < 80)
    if context_window >= 1_000_000:
        grid_width = 5 if is_narrow_screen else 20
    else:
        grid_width = 5 if is_narrow_screen else 10
    grid_height = 10 if context_window >= 1_000_000 else (5 if is_narrow_screen else 10)
    total_squares = grid_width * grid_height

    non_deferred_cats = [cat for cat in cats if not cat.get("isDeferred")]

    category_squares = [
        {
            **cat,
            "squares": _js_round((cat["tokens"] / context_window) * total_squares)
            if cat["name"] == "Free space"
            else max(
                1, _js_round((cat["tokens"] / context_window) * total_squares)
            ),
            "percentageOfTotal": _js_round((cat["tokens"] / context_window) * 100),
        }
        for cat in non_deferred_cats
    ]

    def create_category_squares(category: dict[str, Any]) -> list[dict[str, Any]]:
        squares: list[dict[str, Any]] = []
        exact_squares = (category["tokens"] / context_window) * total_squares
        whole_squares = math.floor(exact_squares)
        fractional_part = exact_squares - whole_squares

        for i in range(category["squares"]):
            square_fullness = 1.0
            if i == whole_squares and fractional_part > 0:
                square_fullness = fractional_part

            squares.append(
                {
                    "color": category["color"],
                    "isFilled": True,
                    "categoryName": category["name"],
                    "tokens": category["tokens"],
                    "percentage": category["percentageOfTotal"],
                    "squareFullness": square_fullness,
                }
            )

        return squares

    grid_squares: list[dict[str, Any]] = []

    reserved_category = next(
        (
            cat
            for cat in category_squares
            if cat["name"] in (RESERVED_CATEGORY_NAME, MANUAL_COMPACT_BUFFER_NAME)
        ),
        None,
    )
    non_reserved_categories = [
        cat
        for cat in category_squares
        if cat["name"]
        not in (RESERVED_CATEGORY_NAME, MANUAL_COMPACT_BUFFER_NAME, "Free space")
    ]

    for cat in non_reserved_categories:
        for square in create_category_squares(cat):
            if len(grid_squares) < total_squares:
                grid_squares.append(square)

    reserved_square_count = reserved_category["squares"] if reserved_category else 0

    free_space_cat = next((c for c in cats if c["name"] == "Free space"), None)
    free_space_target = total_squares - reserved_square_count

    while len(grid_squares) < free_space_target:
        grid_squares.append(
            {
                "color": "promptBorder",
                "isFilled": True,
                "categoryName": "Free space",
                "tokens": (free_space_cat or {}).get("tokens", 0),
                "percentage": _js_round(
                    (free_space_cat["tokens"] / context_window) * 100
                )
                if free_space_cat
                else 0,
                "squareFullness": 1.0,
            }
        )

    if reserved_category:
        for square in create_category_squares(reserved_category):
            if len(grid_squares) < total_squares:
                grid_squares.append(square)

    grid_rows: list[list[dict[str, Any]]] = []
    for i in range(grid_height):
        grid_rows.append(grid_squares[i * grid_width : (i + 1) * grid_width])

    return grid_rows


def _format_message_breakdown(message_breakdown: dict[str, Any]) -> dict[str, Any]:
    """Combine tool calls + results by name, sorted by total tokens descending."""
    tools_map: dict[str, dict[str, int]] = {}

    for name, tokens in message_breakdown["toolCallsByType"].items():
        existing = tools_map.get(name, {"callTokens": 0, "resultTokens": 0})
        tools_map[name] = {**existing, "callTokens": tokens}

    for name, tokens in message_breakdown["toolResultsByType"].items():
        existing = tools_map.get(name, {"callTokens": 0, "resultTokens": 0})
        tools_map[name] = {**existing, "resultTokens": tokens}

    tools_by_type_array = sorted(
        (
            {
                "name": name,
                "callTokens": v["callTokens"],
                "resultTokens": v["resultTokens"],
            }
            for name, v in tools_map.items()
        ),
        key=lambda d: d["callTokens"] + d["resultTokens"],
        reverse=True,
    )

    attachments_by_type_array = sorted(
        (
            {"name": name, "tokens": tokens}
            for name, tokens in message_breakdown["attachmentsByType"].items()
        ),
        key=lambda d: d["tokens"],
        reverse=True,
    )

    return {
        "toolCallTokens": message_breakdown["toolCallTokens"],
        "toolResultTokens": message_breakdown["toolResultTokens"],
        "attachmentTokens": message_breakdown["attachmentTokens"],
        "assistantMessageTokens": message_breakdown["assistantMessageTokens"],
        "userMessageTokens": message_breakdown["userMessageTokens"],
        "toolCallsByType": tools_by_type_array,
        "attachmentsByType": attachments_by_type_array,
    }
