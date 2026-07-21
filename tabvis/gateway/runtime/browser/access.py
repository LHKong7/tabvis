"""Binding-based browser access for tools (design §10.4).

This is the API a rerouted browser tool calls instead of reaching for the manager's ContextVar-resolved
`BrowserService`: it resolves the *active binding* (published by the launcher for the running task) and
drives the page through the process-wide Browser Runtime. The tool never holds a raw browser — it names
an intent, the runtime executes it against the leased binding and returns an observation.

Converting each production browser tool to call :func:`execute_intent` is a mechanical rollout; this
module is the single choke point that rollout targets.
"""

from __future__ import annotations

from typing import Any

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.browser.binding_context import current_binding_id
from tabvis.gateway.runtime.browser.contracts import BrowserIntent, ExecutionRecord
from tabvis.gateway.runtime.browser.runtime import get_browser_runtime


def active_binding() -> str | None:
    """The binding the current task operates under, or None."""
    return current_binding_id()


async def execute_intent(action: str, *, side_effecting: bool = False, **params: Any) -> ExecutionRecord:
    """Drive the active binding's browser with one intent (design §10.4).

    Raises ``BROWSER_BINDING_NOT_FOUND`` when called outside a bound run — a tool must operate under an
    acquired binding, never against an ambient browser.
    """
    binding_id = current_binding_id()
    if binding_id is None:
        raise GatewayError("BROWSER_BINDING_NOT_FOUND", message="No active browser binding for this task")
    runtime = get_browser_runtime()
    return await runtime.execute(binding_id, BrowserIntent(action=action, params=params, side_effecting=side_effecting))
