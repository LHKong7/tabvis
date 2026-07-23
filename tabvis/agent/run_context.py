"""RunContext / SessionLocator — the immutable per-Run locator (Resume Plus §4.1, §4.4).

A Run's identity (who owns it, which agent/session/run it is, and the *resolved* project/session
directory it must read and write) is carried in one immutable object, bound to a
:class:`contextvars.ContextVar`. That is the seam the design calls for: today persistence derives
the transcript / browser-session / artifact / download paths from process-global bootstrap state
(``switch_session``), which two concurrently-running agents can stomp on. A task-local RunContext
lets each writer resolve its paths from *its own* Run instead.

This module only establishes and carries the locator. Migrating each writer to read it (rather than
the global session state) is incremental follow-up work; until a writer is converted, the transition
mechanism remains ``switch_session`` with the resolver's resolved ``project_dir`` (which the Run
context also records, so the two never disagree).

Pure and dependency-light: no I/O, no imports of the heavier runtime, so anything may read the
current Run without a cycle.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class RunContext:
    """Immutable locator for one execution. Also referred to as the SessionLocator in the design.

    ``project_dir`` is the *resolved* on-disk project directory for ``session_id`` — supplied by the
    resolver, not re-derived from the caller's current cwd — so a cross-project resume reads and
    writes the original session's transcript rather than an empty one under the wrong project.
    """

    principal_id: str
    agent_id: str
    session_id: str
    run_id: str
    cwd: str
    project_dir: str | None = None
    resume_mode: str = "fresh"  # fresh | conversation_only | plus


_current: contextvars.ContextVar[RunContext | None] = contextvars.ContextVar(
    "tabvis_run_context", default=None
)


def get_run_context() -> RunContext | None:
    """The RunContext bound to the current task, or None outside a Run."""
    return _current.get()


def set_run_context(ctx: RunContext | None) -> contextvars.Token[RunContext | None]:
    """Bind ``ctx`` as the current Run; returns a token to restore the prior value."""
    return _current.set(ctx)


def reset_run_context(token: contextvars.Token[RunContext | None]) -> None:
    """Restore the RunContext replaced by :func:`set_run_context`."""
    _current.reset(token)


@contextlib.contextmanager
def run_context_scope(ctx: RunContext) -> Iterator[RunContext]:
    """Bind ``ctx`` for the duration of a ``with`` block, restoring the prior value on exit."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
