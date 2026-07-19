"""Agent (Task) tool.

Delegates work to a subagent. The model calls it with ``{description, prompt, subagent_type?}``;
``call`` resolves the agent definition from ``context.options.agent_definitions["activeAgents"]``
(defaulting to :data:`~tabvis.agent.tools.agent_defs.GENERAL_PURPOSE_AGENT`), runs it via
:func:`~tabvis.agent.tools.agent_runner.run_agent` (which reuses the main agent loop), and returns
the subagent's report as a ``tool_result``.

Tool identity: ``name = "Agent"`` (``AGENT_TOOL_NAME``) with the legacy wire name ``"Task"``
(``LEGACY_AGENT_TOOL_NAME``) kept as an alias — so permission rules / hooks / resumed sessions
that reference ``Task`` still match.

Not implemented here: background / async agents, teammates / swarms, worktree isolation, the
fork-subagent path, MCP-required-server gating, agent color, the ``run_in_background`` /
``model`` / ``name`` / ``isolation`` / ``cwd`` input params, the ``async_launched`` /
``teammate_spawned`` result variants, and the agentId/usage trailer. Only the synchronous
``status: 'completed'`` path is implemented.

Casing: Python identifiers snake_case; the result ``data`` dict and the ``tool_result`` block keep
their wire keys (``content`` / ``agentType`` / ``tool_use_id``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.agent.tools.agent_defs import GENERAL_PURPOSE_AGENT, AgentDefinition
from tabvis.agent.tools.agent_runner import run_agent
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.types.message import AssistantMessage

# AGENT_TOOL_NAME is 'Agent'; 'Task' is the LEGACY wire name (kept as an alias for backward-compat
# with permission rules / hooks / resumed sessions).
AGENT_TOOL_NAME = "Agent"
LEGACY_AGENT_TOOL_NAME = "Task"


class AgentToolInput(BaseModel):
    """Validated input for :data:`agent_tool`."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(description="A short (3-5 word) description of the task")
    prompt: str = Field(description="The task for the agent to perform")
    subagent_type: str | None = Field(
        default=None,
        description="The type of specialized agent to use for this task",
    )


def _active_agents(context: ToolUseContext) -> list[AgentDefinition]:
    """Pull ``activeAgents`` off ``context.options.agent_definitions`` (the wire-keyed result)."""
    defs = context.options.agent_definitions
    if isinstance(defs, dict):
        active = defs.get("activeAgents")
        if isinstance(active, list):
            return active
    return []


def _resolve_agent_def(context: ToolUseContext, subagent_type: str | None) -> AgentDefinition:
    """Resolve the agent definition by ``subagent_type`` (defaulting to general-purpose).

    When ``subagent_type`` is omitted the general-purpose agent is used; otherwise it is looked
    up among the active agents and an error is raised if not found.
    """
    effective_type = subagent_type or GENERAL_PURPOSE_AGENT.agent_type
    agents = _active_agents(context)
    if not agents:
        # No agent_definitions threaded through context — fall back to the built-in.
        if effective_type == GENERAL_PURPOSE_AGENT.agent_type:
            return GENERAL_PURPOSE_AGENT
        raise ValueError(
            f"Agent type '{effective_type}' not found. Available agents: "
            f"{GENERAL_PURPOSE_AGENT.agent_type}"
        )

    found = next((a for a in agents if a.agent_type == effective_type), None)
    if found is None:
        available = ", ".join(a.agent_type for a in agents)
        raise ValueError(
            f"Agent type '{effective_type}' not found. Available agents: {available}"
        )
    return found


def _tools_description(agent: AgentDefinition) -> str:
    """Render an agent's tool pool as a short description string (allowlist-only branch)."""
    tools = agent.tools
    if tools is None or tools == ["*"]:
        return "All tools"
    if not tools:
        return "None"
    return ", ".join(tools)


def _format_agent_line(agent: AgentDefinition) -> str:
    """Format one agent-listing line: ``- type: whenToUse (Tools: ...)``."""
    return f"- {agent.agent_type}: {agent.when_to_use} (Tools: {_tools_description(agent)})"


def _get_prompt(agents: list[AgentDefinition]) -> str:
    """Build the tool's prompt text — the inline agent-list description."""
    listing = "\n".join(_format_agent_line(a) for a in agents) if agents else _format_agent_line(
        GENERAL_PURPOSE_AGENT
    )
    return (
        "Launch a new agent to handle complex, multi-step tasks autonomously.\n\n"
        f"The {AGENT_TOOL_NAME} tool launches specialized agents (subprocesses) that "
        "autonomously handle complex tasks. Each agent type has specific capabilities and tools "
        "available to it.\n\n"
        "Available agent types and the tools they have access to:\n"
        f"{listing}\n\n"
        f"When using the {AGENT_TOOL_NAME} tool, specify a subagent_type parameter to select "
        "which agent type to use. If omitted, the general-purpose agent is used.\n\n"
        "Usage notes:\n"
        "- Always include a short description (3-5 words) summarizing what the agent will do\n"
        "- Launch multiple agents concurrently whenever possible, to maximize performance; to do "
        "that, use a single message with multiple tool uses\n"
        "- When the agent is done, it will return a single message back to you. The result "
        "returned by the agent is not visible to the user. To show the user the result, you "
        "should send a text message back to the user with a concise summary of the result.\n"
        "- Each Agent invocation starts fresh — provide a complete task description.\n"
        "- The agent's outputs should generally be trusted\n"
        "- Clearly tell the agent whether you expect it to write code or just to do research."
    )


class AgentTool(Tool):
    """``Agent`` — delegate a task to a subagent."""

    name = AGENT_TOOL_NAME
    aliases = [LEGACY_AGENT_TOOL_NAME]
    search_hint = "delegate work to a subagent"
    input_schema = AgentToolInput
    max_result_size_chars = 100_000

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return "Launch a new agent"

    async def prompt(self, options: dict[str, Any]) -> str:
        agents = _active_agents_from_options(options)
        return _get_prompt(agents)

    def is_read_only(self, input: Any) -> bool:
        # Delegates permission checks to its underlying tools.
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return True

    def get_activity_description(self, input: Any | None) -> str | None:
        if input is None:
            return "Running task"
        description = (
            getattr(input, "description", None)
            if not isinstance(input, dict)
            else input.get("description")
        )
        return description or "Running task"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> dict[str, Any]:
        return {"behavior": "allow", "updatedInput": input}

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[Any]:
        subagent_type = getattr(args, "subagent_type", None)
        prompt = args.prompt

        agent_def = _resolve_agent_def(context, subagent_type)
        report = await run_agent(
            prompt=prompt,
            agent_def=agent_def,
            parent_context=context,
            can_use_tool=can_use_tool,
        )
        return ToolResult(
            data={"content": report, "agentType": agent_def.agent_type}
        )

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        report = data.get("content", "")
        if not report:
            report = "(Subagent completed but returned no output.)"
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": report,
        }


def _active_agents_from_options(options: dict[str, Any]) -> list[AgentDefinition]:
    """Extract active agents from a ``prompt`` options bag (``{agents: [...]}`` if present)."""
    if isinstance(options, dict):
        agents = options.get("agents") or options.get("activeAgents")
        if isinstance(agents, list):
            return agents
    return [GENERAL_PURPOSE_AGENT]


agent_tool = AgentTool()
