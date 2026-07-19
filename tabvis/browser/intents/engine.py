"""Execution Engine + the per-intent handlers (INT-1, extended in INT-4).

The engine dispatches an :class:`Intent` to its registered handler and records the outcome as an
:class:`ExecutionRecord`, minting an ``execution_id``. Each intent has an explicit handler, exactly
as ``design.md`` §"Execution Engine" prescribes (e.g. ``NavigateIntent → ResolveTarget → PolicyCheck
→ PageNavigateCommand → WaitForPageState → EmitObservation``). Every handler decomposes to the
*existing* ``BrowserService`` methods, and every navigation goes through one policy-guarded helper
(:func:`_guarded_navigate`) so the allowlist applies uniformly — even to Search/Compare, which the
low-level ``BrowserNavigate`` tool would not gate.

Handlers registered: ``navigate`` (INT-1), ``search`` / ``research`` / ``compare`` (INT-4), and a
``download`` seam that fails clearly until ``BrowserService`` grows a download primitive. Nothing here
is exposed to the model directly — the flag-gated ``BrowserIntent`` tool (INT-2) and the
:class:`~tabvis.browser.intents.router.IntentRouter` (INT-3) sit on top.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable
from urllib.parse import quote_plus

from tabvis.browser.intents.types import (
    ExecutionRecord,
    Intent,
    IntentBlocked,
    IntentContext,
    IntentName,
)
from tabvis.browser.session import utc_now

# A handler expands one intent into browser operations and returns the observation dict.
IntentHandler = Callable[[Intent, IntentContext], Awaitable[dict[str, Any]]]

_SEARCH_ENGINES = {
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "google": "https://www.google.com/search?q={q}",
}


def _search_url(query: str, engine: str | None = None) -> str:
    template = _SEARCH_ENGINES.get((engine or "duckduckgo").lower(), _SEARCH_ENGINES["duckduckgo"])
    return template.format(q=quote_plus(query))


async def _guarded_navigate(url: str) -> dict[str, Any]:
    """PolicyCheck → navigate → observation. The single choke point every intent navigation uses.

    Runs the same ``check_navigation_permission`` the ``BrowserNavigate`` tool uses, BEFORE the
    browser is launched, so a denied target costs nothing and the allowlist is enforced uniformly.
    """
    from tabvis.browser.manager import get_or_create_browser_service
    from tabvis.agent.tools.browser_common import check_navigation_permission

    decision = check_navigation_permission(
        "BrowserNavigate", {"action": "goto", "url": url}, None  # type: ignore[arg-type]
    )
    if decision.get("behavior") != "allow":
        raise IntentBlocked(decision.get("message") or f"navigation to {url!r} is not permitted")
    service = await get_or_create_browser_service()
    return await service.navigate(url)


async def navigate_handler(intent: Intent, _context: IntentContext) -> dict[str, Any]:
    """NavigateIntent: ResolveTarget → PolicyCheck → navigate → EmitObservation."""
    url = str(intent.params.get("url") or "").strip()
    if not url:
        raise ValueError("navigate intent requires a 'url' param")
    return await _guarded_navigate(url)


async def search_handler(intent: Intent, _context: IntentContext) -> dict[str, Any]:
    """SearchIntent: turn a query into a search-engine navigation and observe the results page."""
    query = str(intent.params.get("query") or "").strip()
    if not query:
        raise ValueError("search intent requires a 'query' param")
    observation = await _guarded_navigate(_search_url(query, intent.params.get("engine")))
    return {**observation, "intent": "search", "query": query}


async def research_handler(intent: Intent, _context: IntentContext) -> dict[str, Any]:
    """ResearchIntent: an entry point — kick off with a search on the topic.

    Real multi-page research is the agent's job across the loop; the intent decomposes to the initial
    search navigation so the agent has somewhere to start reading.
    """
    topic = str(intent.params.get("topic") or intent.params.get("query") or "").strip()
    if not topic:
        raise ValueError("research intent requires a 'topic' param")
    observation = await _guarded_navigate(_search_url(topic, intent.params.get("engine")))
    return {**observation, "intent": "research", "topic": topic}


async def compare_handler(intent: Intent, _context: IntentContext) -> dict[str, Any]:
    """CompareIntent: visit each url in turn (policy-guarded) and summarize what was compared."""
    urls = intent.params.get("urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("compare intent requires a non-empty 'urls' list")
    compared: list[dict[str, Any]] = []
    observation: dict[str, Any] = {}
    for raw_url in urls:
        observation = await _guarded_navigate(str(raw_url))
        compared.append({"url": observation.get("url"), "title": observation.get("title")})
    return {**observation, "intent": "compare", "compared": compared}


async def download_handler(_intent: Intent, _context: IntentContext) -> dict[str, Any]:
    """DownloadIntent — seam only. ``BrowserService`` has no download primitive yet (a later step)."""
    raise NotImplementedError(
        "download intent is not implemented yet — pending a BrowserService.download seam"
    )


class ExecutionEngine:
    """Dispatches intents to per-intent handlers and records the outcome."""

    def __init__(self) -> None:
        self._handlers: dict[str, IntentHandler] = {}

    def register(self, name: str, handler: IntentHandler) -> None:
        self._handlers[name] = handler

    def has(self, name: str) -> bool:
        return name in self._handlers

    def handler_names(self) -> list[str]:
        return sorted(self._handlers)

    async def run(
        self, intent: Intent, context: IntentContext | None = None
    ) -> ExecutionRecord:
        """Run one intent to an :class:`ExecutionRecord`. Never raises — failures land on the record."""
        ctx = context or IntentContext()
        record = ExecutionRecord(execution_id=ctx.execution_id, intent=intent.name)
        handler = self._handlers.get(intent.name)
        if handler is None:
            record.status = "failed"
            record.error = f"no handler registered for intent {intent.name!r}"
            record.ended_at = utc_now()
            return record
        try:
            record.observation = await handler(intent, ctx)
            record.status = "completed"
        except IntentBlocked as e:
            record.status = "blocked"
            record.error = str(e)
        except Exception as e:  # noqa: BLE001 - surface on the record, don't propagate
            record.status = "failed"
            record.error = f"{type(e).__name__}: {e}"
        record.ended_at = utc_now()
        return record


_engine: ExecutionEngine | None = None


def get_execution_engine() -> ExecutionEngine:
    """The process-wide :class:`ExecutionEngine`, with the built-in handlers registered."""
    global _engine
    if _engine is None:
        engine = ExecutionEngine()
        engine.register(IntentName.NAVIGATE, navigate_handler)
        engine.register(IntentName.SEARCH, search_handler)
        engine.register(IntentName.RESEARCH, research_handler)
        engine.register(IntentName.COMPARE, compare_handler)
        engine.register(IntentName.DOWNLOAD, download_handler)
        _engine = engine
    return _engine
