"""Agent context for analytics attribution

Tracks agent identity across async operations without parameter drilling. Two agent types:

1. Subagents (Agent tool): run in-process for quick, delegated tasks. Context shape:
   :class:`SubagentContext` with ``agentType: 'subagent'``.
2. In-process teammates: part of a swarm with team coordination. Context shape:
   :class:`TeammateAgentContext` with ``agentType: 'teammate'``.

For swarm teammates in separate processes (tmux/iTerm2), env vars are used instead:
``TABVIS_AGENT_ID`` / ``TABVIS_PARENT_SESSION_ID``.

WHY a context var (not AppState): when agents are backgrounded (ctrl+b), multiple agents can
run concurrently in the same process. AppState is a single shared state that would be
overwritten, mis-attributing Agent A's events to Agent B. The async-local store isolates each
async execution chain so concurrent agents don't interfere.

stdlib substitution (npm -> PyPI): the TS ``async_hooks.AsyncLocalStorage`` maps to the stdlib
:class:`contextvars.ContextVar`, which provides the same async-local-isolation semantics (each
``asyncio`` task / thread sees its own value; child tasks inherit the parent's snapshot).
``runWithAgentContext`` is implemented via ``ContextVar.set`` + ``ContextVar.reset`` (token),
which is the faithful equivalent of ``AsyncLocalStorage.run(store, fn)`` for a synchronous
callback. (For async callables run them inside ``contextvars.copy_context()`` if isolation is
required; the TS API takes ``() => T`` and is mirrored exactly here.)

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; the context dicts
do NOT round-trip to the transcript/SDK wire form (they live only in-process for analytics),
so they use snake_case keys (``agent_id`` / ``parent_session_id`` / ``agent_type`` …). The
``agent_type`` discriminator values keep their TS literal strings (``'subagent'`` / ``'teammate'``).
The ``invocation_kind`` literals (``'spawn'`` / ``'resume'``) are likewise preserved verbatim.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from typing import Any, Literal, TypedDict, TypeVar

from tabvis.utils.agent_swarms_enabled import is_agent_swarms_enabled

_T = TypeVar("_T")

__all__ = [
    "AgentContext",
    "SubagentContext",
    "TeammateAgentContext",
    "consume_invoking_request_id",
    "get_agent_context",
    "get_subagent_log_name",
    "is_subagent_context",
    "is_teammate_agent_context",
    "run_with_agent_context",
]


class SubagentContext(TypedDict, total=False):
    """Context for subagents (Agent tool agents) — run in-process for quick, delegated tasks."""

    # The subagent's UUID (from create_agent_id()).
    agent_id: str
    # The team lead's session ID (from TABVIS_PARENT_SESSION_ID); absent for main REPL subagents.
    parent_session_id: str
    # Agent type discriminator — always 'subagent' for Agent tool agents.
    agent_type: Literal["subagent"]
    # The subagent's type name (e.g. "Explore", "Bash", "code-reviewer").
    subagent_name: str
    # Whether this is a built-in agent (vs user-defined custom agent).
    is_built_in: bool
    # The request_id in the invoking agent that spawned/resumed this agent. For nested
    # subagents this is the immediate invoker, not the root. Updated on each resume.
    invoking_request_id: str
    # Whether this invocation is the initial spawn or a subsequent resume via SendMessage.
    # Undefined when invoking_request_id is absent.
    invocation_kind: Literal["spawn", "resume"]
    # Mutable flag: has this invocation's edge been emitted to telemetry yet? Reset on each
    # spawn/resume; flipped True by consume_invoking_request_id() on the first terminal event.
    invocation_emitted: bool


class TeammateAgentContext(TypedDict, total=False):
    """Context for in-process teammates — part of a swarm with team coordination."""

    # Full agent ID, e.g. "researcher@my-team".
    agent_id: str
    # Display name, e.g. "researcher".
    agent_name: str
    # Team name this teammate belongs to.
    team_name: str
    # UI color assigned to this teammate.
    agent_color: str
    # Whether teammate must enter plan mode before implementing.
    plan_mode_required: bool
    # The team lead's session ID for transcript correlation.
    parent_session_id: str
    # Whether this agent is the team lead.
    is_team_lead: bool
    # Agent type discriminator — always 'teammate' for swarm teammates.
    agent_type: Literal["teammate"]
    # The request_id in the invoking agent that spawned/resumed this teammate. Undefined for
    # teammates started outside a tool call (e.g. session start). Updated on each resume.
    invoking_request_id: str
    # See SubagentContext.invocation_kind.
    invocation_kind: Literal["spawn", "resume"]
    # Mutable flag: see SubagentContext.invocation_emitted.
    invocation_emitted: bool


# Discriminated union on ``agent_type``. Runtime: a plain dict of one of the two shapes above.
AgentContext = dict[str, Any]


# Async-local store (faithful stand-in for ``new AsyncLocalStorage<AgentContext>()``).
_agent_context_storage: contextvars.ContextVar[AgentContext | None] = contextvars.ContextVar(
    "agent_context", default=None
)


def get_agent_context() -> AgentContext | None:
    """Get the current agent context, if any.

    Returns ``None`` if not running within an agent context (subagent or teammate). Use the
    type guards :func:`is_subagent_context` / :func:`is_teammate_agent_context` to narrow.
    """
    return _agent_context_storage.get()


def run_with_agent_context(context: AgentContext, fn: Callable[[], _T]) -> _T:
    """Run ``fn`` with the given agent context.

    All operations within ``fn`` (and child tasks copying this context) see ``context``.
    Mirrors ``AsyncLocalStorage.run(context, fn)`` via ContextVar set/reset.
    """
    token = _agent_context_storage.set(context)
    try:
        return fn()
    finally:
        _agent_context_storage.reset(token)


def is_subagent_context(context: AgentContext | None) -> bool:
    """Type guard: whether ``context`` is a SubagentContext."""
    return context is not None and context.get("agent_type") == "subagent"


def is_teammate_agent_context(context: AgentContext | None) -> bool:
    """Type guard: whether ``context`` is a TeammateAgentContext.

    Gated on :func:`tabvis.utils.agent_swarms_enabled.is_agent_swarms_enabled` — returns ``False``
    when swarms are disabled (faithful to the TS).
    """
    if is_agent_swarms_enabled():
        return context is not None and context.get("agent_type") == "teammate"
    return False


def get_subagent_log_name() -> str | None:
    """The subagent name suitable for analytics logging.

    Returns the agent type name for built-in agents, ``"user-defined"`` for custom agents, or
    ``None`` if not running within a subagent context. Safe for analytics metadata: built-in
    names are code constants, and custom agents always map to the literal ``"user-defined"``.
    """
    context = get_agent_context()
    if not is_subagent_context(context) or not context.get("subagent_name"):
        return None
    return context["subagent_name"] if context.get("is_built_in") else "user-defined"


class _ConsumedInvokingRequestId(TypedDict):
    invoking_request_id: str
    invocation_kind: Literal["spawn", "resume"] | None


def consume_invoking_request_id() -> _ConsumedInvokingRequestId | None:
    """Get the invoking request_id for the current agent context — once per invocation.

    Returns the id on the first call after a spawn/resume, then ``None`` until the next
    boundary. Also ``None`` on the main thread or when the spawn path had no request_id.

    Sparse-edge semantics: ``invoking_request_id`` appears on exactly one
    ``tengu_api_success``/``error`` per invocation, so a non-NULL value downstream marks a
    spawn/resume boundary. Flips the mutable ``invocation_emitted`` flag in place.
    """
    context = get_agent_context()
    if context is None or not context.get("invoking_request_id") or context.get(
        "invocation_emitted"
    ):
        return None
    context["invocation_emitted"] = True
    return {
        "invoking_request_id": context["invoking_request_id"],
        "invocation_kind": context.get("invocation_kind"),
    }
