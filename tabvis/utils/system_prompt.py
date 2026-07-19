"""System-prompt types and built-in-agent detection helpers.

Builds the effective system prompt array based on priority:
0. Override system prompt (if set, e.g., via loop mode — REPLACES all other prompts)
1. Agent system prompt (if ``main_thread_agent_definition`` is set)
2. Custom system prompt (if specified via ``--system-prompt``)
3. Default system prompt (the standard Tabvis prompt)

Plus ``append_system_prompt`` is always added at the end if specified (except when override is set).
"""

from __future__ import annotations

from typing import Any

from tabvis.utils.system_prompt_type import SystemPrompt, as_system_prompt

__all__ = ["SystemPrompt", "as_system_prompt", "build_effective_system_prompt"]


def _is_built_in_agent(agent: Any) -> bool:
    """``Agent.source === 'built-in'``."""
    return getattr(agent, "source", None) == "built-in"


def build_effective_system_prompt(
    *,
    main_thread_agent_definition: Any | None,
    tool_use_context: Any,
    custom_system_prompt: str | None,
    default_system_prompt: list[str],
    append_system_prompt: str | None,
    override_system_prompt: str | None = None,
) -> SystemPrompt:
    """Build the effective system prompt.

    ``main_thread_agent_definition`` is an ``AgentDefinition``-shaped object that exposes
    ``get_system_prompt`` (a built-in agent takes a ``tool_use_context`` kwarg), plus optional
    ``memory`` / ``agent_type`` attributes. Kept as ``Any`` so this stays decoupled from the
    flat ``tabvis.agent.tools.agent_tool`` surface (matches the loose TS structural typing).
    """
    if override_system_prompt:
        return as_system_prompt([override_system_prompt])

    agent_system_prompt: str | None = None
    if main_thread_agent_definition is not None:
        # ``isBuiltInAgent`` (loadAgentsDir.ts) is just ``agent.source === 'built-in'`` — inlined
        # here to stay decoupled from the flat ``tabvis.agent.tools.agent_*`` surface. A built-in agent's
        # ``get_system_prompt`` takes the ``tool_use_context`` kwarg; a dir agent's takes none.
        if _is_built_in_agent(main_thread_agent_definition):
            agent_system_prompt = main_thread_agent_definition.get_system_prompt(
                tool_use_context={"options": tool_use_context.options}
            )
        else:
            agent_system_prompt = main_thread_agent_definition.get_system_prompt()

    if agent_system_prompt:
        head = [agent_system_prompt]
    elif custom_system_prompt:
        head = [custom_system_prompt]
    else:
        head = list(default_system_prompt)

    return as_system_prompt([*head, *([append_system_prompt] if append_system_prompt else [])])
