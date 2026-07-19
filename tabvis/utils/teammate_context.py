"""In-process teammate runtime context

Provides AsyncLocalStorage-based context for in-process teammates, enabling concurrent
teammate execution without global state conflicts. The Python analogue of Node's
``AsyncLocalStorage`` is :class:`contextvars.ContextVar` (captured at invocation time,
propagates across awaits, isolated per task).

Relationship with other teammate identity mechanisms:
- Env vars (``TABVIS_AGENT_ID``): process-based teammates spawned via tmux.
- ``dynamic_team_context`` (teammate.py): process-based teammates joining at runtime.
- :class:`TeammateContext` (this file): in-process teammates via ContextVar.

The helper functions in teammate.py check the ContextVar first, then ``dynamic_team_context``,
then env vars.

Casing: Python identifiers are snake_case. :class:`TeammateContext` is a runtime context
object (NOT a JSON/API/transcript wire dict), so its fields are snake_case — there are no wire
keys to preserve. The TS discriminator ``isInProcess: true`` (a compile-time literal) maps to
the always-``True`` :attr:`TeammateContext.is_in_process` field.

Abort: the TS ``AbortController`` is reused verbatim from ``tabvis.utils.abort`` — NOT reinvented.

Stdlib name note: this module is ``tabvis.utils.teammate_context``; ``import contextvars`` below
resolves to the stdlib.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

from tabvis.utils.abort import AbortController

_T = TypeVar("_T")


@dataclass
class TeammateContext:
    """Runtime context for in-process teammates (stored in a ContextVar for concurrent access).

    ``Is_in_process`` is the discriminator (always
    ``True`` for in-process teammates).
    """

    # Full agent ID, e.g. "researcher@my-team".
    agent_id: str
    # Display name, e.g. "researcher".
    agent_name: str
    # Team name this teammate belongs to.
    team_name: str
    # Whether teammate must enter plan mode before implementing.
    plan_mode_required: bool
    # Leader's session ID (for transcript correlation).
    parent_session_id: str
    # Abort controller for lifecycle management (linked to parent).
    abort_controller: AbortController
    # UI color assigned to this teammate.
    color: str | None = None
    # Discriminator - always true for in-process teammates.
    is_in_process: Literal[True] = True


_teammate_context_storage: contextvars.ContextVar[TeammateContext | None] = (
    contextvars.ContextVar("tabvis_teammate_context", default=None)
)


def get_teammate_context() -> TeammateContext | None:
    """Get the current in-process teammate context, or ``None`` if not running as one.

    Return the teammate context.
    """
    return _teammate_context_storage.get()


def run_with_teammate_context(context: TeammateContext, fn: Callable[[], _T]) -> _T:
    """Run ``fn`` with ``context`` set as the active in-process teammate context.

    Used when spawning an in-process teammate to establish its execution context.
    ``runWithTeammateContext`` (``teammateContextStorage.run(context, fn)``).
    """
    ctx = contextvars.copy_context()
    return ctx.run(_run_with_context_set, context, fn)


def _run_with_context_set(context: TeammateContext, fn: Callable[[], _T]) -> _T:
    """Set the teammate-context var in the active (copied) context, then call ``fn``."""
    _teammate_context_storage.set(context)
    return fn()


def is_in_process_teammate() -> bool:
    """Whether current execution is within an in-process teammate.

    Faster than ``get_teammate_context() is not None`` for simple checks.
    ``isInProcessTeammate``.
    """
    return _teammate_context_storage.get() is not None


def create_teammate_context(
    *,
    agent_id: str,
    agent_name: str,
    team_name: str,
    plan_mode_required: bool,
    parent_session_id: str,
    abort_controller: AbortController,
    color: str | None = None,
) -> TeammateContext:
    """Create a :class:`TeammateContext` from spawn configuration.

    The ``abort_controller`` is passed in by the caller. For in-process teammates this is
    typically an independent controller (not linked to parent) so teammates continue running
    when the leader's query is interrupted. Returns a complete context with
    ``is_in_process=True``.
    """
    return TeammateContext(
        agent_id=agent_id,
        agent_name=agent_name,
        team_name=team_name,
        plan_mode_required=plan_mode_required,
        parent_session_id=parent_session_id,
        abort_controller=abort_controller,
        color=color,
        is_in_process=True,
    )
