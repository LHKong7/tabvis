"""IntentRouter (INT-3).

The design's Intent Router (``design.md`` §"Intent Router"): it receives an :class:`Intent`, resolves
the run context (the ``agent_id`` from the current-agent ContextVar, the ``workspace_id`` from the
agent's workspace), builds an :class:`IntentContext` (which mints the ``execution_id``), and dispatches
to the :class:`~tabvis.browser.intents.engine.ExecutionEngine`.

Capability / Identity-Binding checks belong here per the design, but the capability model and the
Policy Guard are Phase 4 (IDP-8) — for now the per-intent handlers run the navigation PolicyCheck
themselves (``engine._guarded_navigate``), so every intent navigation is still gated. This module is
the seam where the capability check will slot in.
"""

from __future__ import annotations

import os

from tabvis.browser.intents.engine import ExecutionEngine, get_execution_engine
from tabvis.browser.intents.types import ExecutionRecord, Intent, IntentContext


def is_browser_intents_enabled() -> bool:
    """Whether the semantic-intent surface is exposed to the model. ``TABVIS_BROWSER_INTENTS`` (default OFF)."""
    val = os.environ.get("TABVIS_BROWSER_INTENTS")
    return bool(val) and val.strip().lower() not in ("0", "false", "no", "off", "")


class IntentRouter:
    """Builds the run context for an intent and dispatches it to the execution engine."""

    def __init__(self, engine: ExecutionEngine | None = None) -> None:
        self._engine = engine or get_execution_engine()

    async def route(
        self,
        intent: Intent,
        *,
        agent_id: str | None = None,
        workspace_id: str | None = None,
    ) -> ExecutionRecord:
        """Resolve context (agent / workspace), then run the intent. ``execution_id`` is minted here."""
        resolved_agent = agent_id or _current_agent_id()
        resolved_workspace = workspace_id or _workspace_id_for(resolved_agent)
        context = IntentContext(agent_id=resolved_agent, workspace_id=resolved_workspace)
        record = await self._engine.run(intent, context)
        # INT-6: register the execution so GET /v1/executions/{id} can return it.
        try:
            from tabvis.browser.intents.execution_registry import get_execution_registry

            get_execution_registry().record(record)
        except Exception:  # noqa: BLE001 - registry is best-effort
            pass
        return record


def _current_agent_id() -> str | None:
    try:
        from tabvis.browser.manager import current_agent_id

        return current_agent_id()
    except Exception:  # noqa: BLE001
        return None


def _workspace_id_for(agent_id: str | None) -> str | None:
    if not agent_id:
        return None
    try:
        from tabvis.browser import workspace as ws

        record = ws.get_workspace_for_agent(agent_id)
        return record.workspace_id if record is not None else None
    except Exception:  # noqa: BLE001
        return None


_router: IntentRouter | None = None


def get_intent_router() -> IntentRouter:
    """The process-wide :class:`IntentRouter`."""
    global _router
    if _router is None:
        _router = IntentRouter()
    return _router
