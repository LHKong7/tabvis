"""Teammate utilities for agent swarm coordination

These helpers identify whether this Tabvis instance is running as a spawned teammate in a swarm.
Teammates receive their identity via CLI arguments (``--agent-id``, ``--team-name``, etc.) which
are stored in :data:`_dynamic_team_context`. For in-process teammates (running in the same
process), a ContextVar (``teammate_context.py``) provides isolated context per teammate,
preventing concurrent overwrites.

Priority order for identity resolution:
1. ContextVar (in-process teammates) — via ``teammate_context.py``.
2. :data:`_dynamic_team_context` (tmux teammates via CLI args).

Casing: Python identifiers are snake_case. The dynamic-team-context object is an in-process
runtime value object (set from CLI args, consumed in-process — NOT a JSON/API/transcript wire
payload), so its fields are snake_case, matching :class:`~tabvis.utils.teammate_context.TeammateContext`.
The ``AppState.tasks`` dicts ARE wire-shaped (camelCase keys ``type``/``status``/``isIdle``/
``onIdleCallbacks``) and are read by their verbatim wire keys.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Re-export in-process teammate utilities from teammate_context.py (mirrors the TS re-export).
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.teammate_context import (
    TeammateContext as TeammateContext,  # noqa: PLC0414 - re-export
)
from tabvis.utils.teammate_context import (
    create_teammate_context as create_teammate_context,  # noqa: PLC0414 - re-export
)
from tabvis.utils.teammate_context import (
    get_teammate_context,
)
from tabvis.utils.teammate_context import (
    is_in_process_teammate as is_in_process_teammate,  # noqa: PLC0414 - re-export
)
from tabvis.utils.teammate_context import (
    run_with_teammate_context as run_with_teammate_context,  # noqa: PLC0414 - re-export
)

__all__ = [
    "DynamicTeamContext",
    "TeammateContext",
    "clear_dynamic_team_context",
    "create_teammate_context",
    "get_agent_id",
    "get_agent_name",
    "get_dynamic_team_context",
    "get_parent_session_id",
    "get_team_name",
    "get_teammate_color",
    "get_teammate_context",
    "has_active_in_process_teammates",
    "has_working_in_process_teammates",
    "is_in_process_teammate",
    "is_plan_mode_required",
    "is_team_lead",
    "is_teammate",
    "run_with_teammate_context",
    "set_dynamic_team_context",
    "wait_for_teammates_to_become_idle",
]


@dataclass
class DynamicTeamContext:
    """Dynamic team context for runtime team joining (set from CLI args at startup).

    When set, these values take precedence over environment variables. In-process runtime value
    object — snake_case fields (the TS literal used camelCase ``agentId``/``agentName``/… but it
    never round-trips to JSON/API/transcript, so only the Python identifiers change).
    """

    agent_id: str
    agent_name: str
    team_name: str
    plan_mode_required: bool
    color: str | None = None
    parent_session_id: str | None = None


# Module-level mutable singleton (TS ``let dynamicTeamContext``).
_dynamic_team_context: DynamicTeamContext | None = None


def get_parent_session_id() -> str | None:
    """Parent session ID for this teammate (the team lead's session ID for in-process).

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux teammates).
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.parent_session_id
    return _dynamic_team_context.parent_session_id if _dynamic_team_context else None


def set_dynamic_team_context(context: DynamicTeamContext | None) -> None:
    """Set the dynamic team context (called when joining a team at runtime)."""
    global _dynamic_team_context
    _dynamic_team_context = context


def clear_dynamic_team_context() -> None:
    """Clear the dynamic team context (called when leaving a team)."""
    global _dynamic_team_context
    _dynamic_team_context = None


def get_dynamic_team_context() -> DynamicTeamContext | None:
    """Get the current dynamic team context (for inspection/debugging)."""
    return _dynamic_team_context


def get_agent_id() -> str | None:
    """Agent ID if this session is running as a teammate in a swarm, else ``None``.

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux via CLI args).
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.agent_id
    return _dynamic_team_context.agent_id if _dynamic_team_context else None


def get_agent_name() -> str | None:
    """Agent name if this session is running as a teammate in a swarm.

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux via CLI args).
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.agent_name
    return _dynamic_team_context.agent_name if _dynamic_team_context else None


def get_team_name(team_context: dict[str, Any] | None = None) -> str | None:
    """Team name if this session is part of a team.

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux via CLI args) >
    passed ``team_context``. Pass ``team_context`` from AppState to support leaders who don't have
    a dynamic team context set. ``team_context`` is a wire-shaped dict read by its ``teamName`` key.
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.team_name
    if _dynamic_team_context and _dynamic_team_context.team_name:
        return _dynamic_team_context.team_name
    return team_context.get("teamName") if team_context else None


def is_teammate() -> bool:
    """Whether this session is running as a teammate in a swarm.

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux via CLI args). For
    tmux teammates, requires BOTH an agent ID AND a team name.
    """
    # In-process teammates run within the same process.
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return True
    # Tmux teammates require both agent ID and team name.
    return bool(
        _dynamic_team_context
        and _dynamic_team_context.agent_id
        and _dynamic_team_context.team_name
    )


def get_teammate_color() -> str | None:
    """The teammate's assigned color, or ``None`` if not a teammate / no color assigned.

    Priority: ContextVar (in-process) > :data:`_dynamic_team_context` (tmux teammates).
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.color
    return _dynamic_team_context.color if _dynamic_team_context else None


def is_plan_mode_required() -> bool:
    """Whether this teammate session requires plan mode before implementation.

    Priority: ContextVar > :data:`_dynamic_team_context` > ``TABVIS_PLAN_MODE_REQUIRED`` env var.
    """
    in_process_ctx = get_teammate_context()
    if in_process_ctx:
        return in_process_ctx.plan_mode_required
    if _dynamic_team_context is not None:
        return _dynamic_team_context.plan_mode_required
    return is_env_truthy(os.environ.get("TABVIS_PLAN_MODE_REQUIRED"))


def is_team_lead(team_context: dict[str, Any] | None) -> bool:
    """Whether this session is a team lead.

    A session is the team lead if a team context with a ``leadAgentId`` exists AND either our
    agent ID matches the ``leadAgentId`` OR we have no agent ID set (backwards compat: the
    original session that created the team before agent IDs were standardized). ``team_context``
    is a wire-shaped dict read by its ``leadAgentId`` key.
    """
    if not team_context or not team_context.get("leadAgentId"):
        return False

    # Use get_agent_id() for ContextVar support (in-process teammates).
    my_agent_id = get_agent_id()
    lead_agent_id = team_context.get("leadAgentId")

    # If my agent ID matches the lead agent ID, I'm the lead.
    if my_agent_id == lead_agent_id:
        return True

    # Backwards compat: if no agent ID is set and we have a team context, this is the original
    # session that created the team (the lead).
    if not my_agent_id:
        return True

    return False


def has_active_in_process_teammates(app_state: dict[str, Any]) -> bool:
    """Whether there are any running in-process teammate tasks.

    Used by headless/print mode to decide whether to wait for teammates before exiting. The
    ``app_state["tasks"]`` values are wire dicts (camelCase keys: ``type``/``status``).
    """
    for task in app_state.get("tasks", {}).values():
        if (
            task.get("type") == "in_process_teammate"
            and task.get("status") == "running"
        ):
            return True
    return False


def has_working_in_process_teammates(app_state: dict[str, Any]) -> bool:
    """Whether any in-process teammate is running but NOT idle (still processing).

    Used to decide whether to wait before sending shutdown prompts. ``app_state["tasks"]``
    values are wire dicts (camelCase keys ``type``/``status``/``isIdle``).
    """
    for task in app_state.get("tasks", {}).values():
        if (
            task.get("type") == "in_process_teammate"
            and task.get("status") == "running"
            and not task.get("isIdle")
        ):
            return True
    return False


def wait_for_teammates_to_become_idle(
    set_app_state: Callable[[Callable[[dict[str, Any]], dict[str, Any]]], None],
    app_state: dict[str, Any],
) -> Any:
    """Return an awaitable that resolves when all working in-process teammates become idle.

    Registers ``on_idle`` callbacks on each working teammate's task (they invoke these when they
    go idle). Returns an already-resolved Future immediately if no teammates are working. The
    ``app_state["tasks"]`` values are wire dicts; ``onIdleCallbacks`` is appended onto each.

    The TS ``Promise<void>`` maps to an
    :class:`asyncio.Future`; ``set_app_state`` is the store updater (camelCase wire keys).
    """
    import asyncio

    working_task_ids: list[str] = []
    for task_id, task in app_state.get("tasks", {}).items():
        if (
            task.get("type") == "in_process_teammate"
            and task.get("status") == "running"
            and not task.get("isIdle")
        ):
            working_task_ids.append(task_id)

    future: asyncio.Future[None] = asyncio.get_event_loop().create_future()

    if len(working_task_ids) == 0:
        future.set_result(None)
        return future

    remaining = len(working_task_ids)

    def on_idle() -> None:
        nonlocal remaining
        remaining -= 1
        if remaining == 0 and not future.done():
            future.set_result(None)

    # Register callback on each working teammate. Check the current isIdle state to handle a race
    # where a teammate became idle between our snapshot and this registration.
    def updater(prev: dict[str, Any]) -> dict[str, Any]:
        new_tasks = {**prev.get("tasks", {})}
        for task_id in working_task_ids:
            task = new_tasks.get(task_id)
            if task and task.get("type") == "in_process_teammate":
                # If the task is already idle, call on_idle immediately.
                if task.get("isIdle"):
                    on_idle()
                else:
                    new_tasks[task_id] = {
                        **task,
                        "onIdleCallbacks": [
                            *(task.get("onIdleCallbacks") or []),
                            on_idle,
                        ],
                    }
        return {**prev, "tasks": new_tasks}

    set_app_state(updater)
    return future
