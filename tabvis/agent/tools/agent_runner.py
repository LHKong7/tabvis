"""Subagent runner.

Runs a subagent by REUSING the main agent loop (:func:`tabvis.agent.query.query`): it resolves
the subagent's tool pool, builds the agent-driven system prompt, forks a SUBAGENT
:class:`~tabvis.tool.ToolUseContext` (SHARES the parent's abort controller for the sync path, own
``agent_id``/``agent_type``, incremented chain depth, empty messages), then drives ``query`` to
completion and returns the final assistant text as the report. A :data:`MAX_AGENT_DEPTH` recursion
guard refuses to nest beyond the limit.

Not implemented here: agent memory/snapshot, color manager, resume, the fork-subagent path
(which DOES mint a fresh controller), streaming-to-parent progress, max-budget, agent-specific
MCP servers, frontmatter hooks/skills, worktree isolation, sidechain transcript recording, and
env-details prompt enhancement. The runner runs SYNC only and returns the report string.

Casing: Python identifiers snake_case; the app-state dict keeps its wire keys
(``toolPermissionContext`` / ``mcp``).
"""

from __future__ import annotations

import secrets
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from tabvis.constants.prompts import get_system_prompt
from tabvis.agent.query import QueryParams, Terminal, query
from tabvis.agent.query.deps import QueryDeps, production_deps
from tabvis.tool import (
    QueryChainTracking,
    Tool,
    ToolUseContext,
    find_tool_by_name,
    get_empty_tool_permission_context,
)
from tabvis.utils.messages import create_user_message

if TYPE_CHECKING:
    from tabvis.agent.tools.agent_defs import AgentDefinition
    from tabvis.types.can_use_tool import CanUseToolFn

__all__ = ["MAX_AGENT_DEPTH", "create_agent_id", "run_agent"]

# Recursion guard: max nesting depth for subagents (depth 0 = main thread). Prevents unbounded
# Agent→Agent recursion once the Agent tool is in the base pool (verify guard-rail (c)).
MAX_AGENT_DEPTH = 5


def create_agent_id(label: str | None = None) -> str:
    """Generate a unique agent id: ``a{label-}{16 hex}``."""
    suffix = secrets.token_hex(8)
    return f"a{label}-{suffix}" if label else f"a{suffix}"


def _permission_context_from_state(parent_context: ToolUseContext) -> Any:
    """Read the parent app state's ``toolPermissionContext`` (or an empty one)."""
    get_state = parent_context.get_app_state
    app_state = get_state() if callable(get_state) else None
    if isinstance(app_state, dict) and app_state.get("toolPermissionContext"):
        return app_state["toolPermissionContext"]
    return get_empty_tool_permission_context()


def _resolve_agent_tools(agent_def: AgentDefinition, parent_context: ToolUseContext) -> list[Tool]:
    """Resolve the subagent's tool pool.

    ``tools == ["*"]`` (or ``None`` = all tools) -> the full built-in pool via :func:`get_tools`
    under the parent's permission context. Otherwise the named subset filtered out of
    :func:`get_all_base_tools` (preserving the requested order, dropping unknown names).
    """
    # Lazy import to avoid a cycle (tabvis.agent.tools.__init__ registers the Agent tool, which imports
    # this module via agent_tool).
    from tabvis.agent.tools import get_all_base_tools, get_tools

    permission_context = _permission_context_from_state(parent_context)
    wanted = agent_def.tools
    if wanted is None or wanted == ["*"]:
        return list(get_tools(permission_context))

    base = get_all_base_tools()
    resolved: list[Tool] = []
    for name in wanted:
        tool = find_tool_by_name(base, name)
        if tool is not None and tool not in resolved:
            resolved.append(tool)
    return resolved


async def run_agent(
    *,
    prompt: str,
    agent_def: AgentDefinition,
    parent_context: ToolUseContext,
    can_use_tool: CanUseToolFn,
    deps: QueryDeps | None = None,
    model: str | None = None,
) -> str:
    """Run ``agent_def`` against ``prompt`` and return the subagent's final report text.

    Reuses :func:`tabvis.agent.query.query`: builds a forked SUBAGENT context, runs the loop, and
    returns the LAST assistant text block (the report — the subagent's final assistant text,
    relayed back to the caller).
    """
    # 1. Resolve the subagent's tool pool + effective model.
    tools = _resolve_agent_tools(agent_def, parent_context)
    effective_model = model or agent_def.model or parent_context.options.main_loop_model

    # 2. Build the agent-driven system prompt: the agent's own prompt drives the subagent, and is
    #    prepended to the default get_system_prompt(tools, model) so the subagent still gets
    #    tool/env guidance, with the agent prompt first.
    agent_prompt = agent_def.get_system_prompt()
    base_prompt = await get_system_prompt(
        tools,
        effective_model,
        include_project_instructions=not agent_def.omit_tabvis_md,
    )
    system_prompt = [agent_prompt, *base_prompt]

    # 3. Recursion guard: refuse to nest beyond MAX_AGENT_DEPTH (verify guard-rail (c)).
    parent_depth = parent_context.query_tracking.depth if parent_context.query_tracking else 0
    if parent_depth >= MAX_AGENT_DEPTH:
        return (
            f"Maximum subagent depth ({MAX_AGENT_DEPTH}) reached; refusing to spawn another "
            "subagent. Complete the task directly with the tools available."
        )

    # 4. Fork into a SUBAGENT context: own tools/model, fresh agent_id/agent_type, empty messages,
    #    and an incremented chain depth. The SYNC subagent SHARES the parent's AbortController (it
    #    is NOT replaced) so a parent cancel / Ctrl-C cascades to the in-flight subagent (only
    #    async/background agents mint a fresh controller). setAppState is kept (the sync subagent
    #    shares the root store).
    sub_options = replace(
        parent_context.options,
        tools=tools,
        main_loop_model=effective_model,
    )
    sub_context = replace(
        parent_context,
        options=sub_options,
        agent_id=create_agent_id(),
        agent_type=agent_def.agent_type,
        messages=[],
        query_tracking=QueryChainTracking(
            chain_id=(
                parent_context.query_tracking.chain_id
                if parent_context.query_tracking
                else create_agent_id()
            ),
            depth=parent_depth + 1,
        ),
    )

    # 5. Run the loop, collecting the last assistant text as the running report.
    messages = [create_user_message(content=prompt)]
    params = QueryParams(
        messages=messages,
        system_prompt=system_prompt,
        tools=tools,
        can_use_tool=can_use_tool,
        tool_use_context=sub_context,
        deps=deps or production_deps,
    )

    report = ""
    async for item in query(params):
        if isinstance(item, Terminal):
            break
        if isinstance(item, dict) and item.get("type") == "assistant":
            text = _last_text_block(item)
            if text is not None:
                report = text
    return report


def _last_text_block(assistant_message: dict[str, Any]) -> str | None:
    """Return the LAST ``text`` block of an assistant message (the report fragment), or ``None``."""
    content = (assistant_message.get("message") or {}).get("content") or []
    last: str | None = None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            last = block.get("text", "")
    return last
