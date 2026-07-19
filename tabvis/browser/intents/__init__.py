"""Intent layer — the additive Intent seam above the low-level browser tools (INT-1).

``design.md`` §4 wants the agent to drive *Intents* (Research / Search / Navigate / Download /
Compare) rather than low-level Click/Type, with an Intent Router that assigns an ``execution_id`` and
an Execution Engine that expands each intent into browser operations. This package introduces that
seam ABOVE the existing five ref-level tools and ``BrowserService`` methods — it does not replace
them. INT-1 lands the primitives (:class:`~tabvis.browser.intents.types.Intent`,
``IntentContext``, ``ExecutionRecord``, ``new_execution_id``) and one real handler (NavigateIntent),
runnable in-process with no model-facing tool. Exposing an intent tool to the model is INT-2; the
full IntentRouter with capability checks and the ``/executions`` endpoints is INT-3/INT-6.
"""

from __future__ import annotations

from tabvis.browser.intents.engine import (
    ExecutionEngine,
    get_execution_engine,
    navigate_handler,
)
from tabvis.browser.intents.execution_registry import (
    ExecutionRegistry,
    get_execution_registry,
    is_retryable,
)
from tabvis.browser.intents.router import (
    IntentRouter,
    get_intent_router,
    is_browser_intents_enabled,
)
from tabvis.browser.intents.types import (
    ExecutionRecord,
    Intent,
    IntentBlocked,
    IntentContext,
    IntentName,
    new_execution_id,
)

__all__ = [
    "Intent",
    "IntentContext",
    "IntentName",
    "ExecutionRecord",
    "IntentBlocked",
    "new_execution_id",
    "ExecutionEngine",
    "get_execution_engine",
    "navigate_handler",
    "IntentRouter",
    "get_intent_router",
    "is_browser_intents_enabled",
    "ExecutionRegistry",
    "get_execution_registry",
    "is_retryable",
]
