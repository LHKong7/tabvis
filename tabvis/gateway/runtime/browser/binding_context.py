"""The active browser binding for the running task (design §10.4).

Agent tools receive a ``binding_id``, not a raw browser (design §10.4). Rather than thread it through
every tool signature, the launcher publishes the run's acquired binding on a ContextVar for the
duration of the model loop; a browser tool reads :func:`current_binding_id` and drives the page through
the Browser Runtime (see :mod:`tabvis.gateway.runtime.browser.access`). This is the same ContextVar
mechanism the legacy path uses for ``agent_id`` — but the value is now a leased binding, so browser
access is gated by an explicit, observable claim.
"""

from __future__ import annotations

import contextvars

_active_binding: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tabvis_active_binding_id", default=None
)


def bind_binding(binding_id: str) -> contextvars.Token[str | None]:
    """Publish ``binding_id`` as the active binding for this task; returns a token to restore."""
    return _active_binding.set(binding_id)


def unbind_binding(token: contextvars.Token[str | None]) -> None:
    _active_binding.reset(token)


def current_binding_id() -> str | None:
    """The binding the current task operates under, or None outside a bound run."""
    return _active_binding.get()
