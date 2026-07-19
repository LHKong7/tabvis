"""BrowserIntent — drive the browser by *intent* rather than by low-level click (INT-2).

This is the model-facing surface for the Intent layer (``design.md`` §4): instead of composing
``BrowserNavigate``/``Click``/``Type`` itself, the agent states a semantic intent — ``navigate`` /
``search`` / ``research`` / ``compare`` — and the runtime decomposes it (via the
:class:`~tabvis.browser.intents.router.IntentRouter` → ``ExecutionEngine``) into the existing
``BrowserService`` operations, minting an ``execution_id`` and returning the resulting page snapshot.

It is **flag-gated** (``TABVIS_BROWSER_INTENTS``): :meth:`is_enabled` returns False by default, so the
tool is filtered out of the model's tool set and the five low-level tools remain the only surface —
zero change to the default agent. Turn the flag on to let the model drive intents alongside them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tabvis.constants.tools import BROWSER_INTENT_TOOL_NAME
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)

_DESCRIPTION = """Drive the browser by intent instead of low-level clicks.

State what you want to accomplish; the runtime decomposes it into browser operations and returns an
accessibility snapshot of the resulting page (same [ref=eN] format as BrowserNavigate).

Intents:
 - navigate  — open a URL. Requires `url`.
 - search    — search the web for `query` (optional `engine`: duckduckgo | bing | google).
 - research  — start researching a `topic` (kicks off with a search you can then read and follow).
 - compare   — visit each URL in `urls` in turn and summarize them.

Every navigation is checked against the domain allowlist, exactly like BrowserNavigate. The
low-level BrowserNavigate / BrowserClick / BrowserType tools remain available for fine-grained work."""


class BrowserIntentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["navigate", "search", "research", "compare"] = Field(
        description="The semantic intent to perform."
    )
    url: str | None = Field(default=None, description="Target URL (intent='navigate').")
    query: str | None = Field(default=None, description="Search query (intent='search').")
    topic: str | None = Field(default=None, description="Research topic (intent='research').")
    urls: list[str] | None = Field(default=None, description="URLs to compare (intent='compare').")
    engine: str | None = Field(
        default=None, description="Search engine for search/research: duckduckgo | bing | google."
    )


class BrowserIntentTool(Tool):
    name = BROWSER_INTENT_TOOL_NAME
    search_hint = "browser: drive the browser by intent — navigate, search, research, compare"
    input_schema = BrowserIntentInput
    should_defer = False
    always_load = True
    max_result_size_chars = 48_000

    def is_enabled(self) -> bool:
        # Gated: only exposed to the model when the intent surface is turned on.
        from tabvis.browser.intents.router import is_browser_intents_enabled

        return playwright_available() and is_browser_intents_enabled()

    def is_read_only(self, input: Any) -> bool:
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    def interrupt_behavior(self) -> str:
        return "cancel"

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        intent = input.get("intent") if isinstance(input, dict) else getattr(input, "intent", None)
        return f"Browser intent: {intent}" if intent else "Browser intent"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserIntent"

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def call(
        self,
        args: BrowserIntentInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        from tabvis.browser.intents import Intent
        from tabvis.browser.intents.router import get_intent_router

        params = {
            k: v
            for k, v in {
                "url": args.url,
                "query": args.query,
                "topic": args.topic,
                "urls": args.urls,
                "engine": args.engine,
            }.items()
            if v is not None
        }
        record = await get_intent_router().route(
            Intent(name=args.intent, params=params), agent_id=context.agent_id
        )
        if record.status == "completed" and isinstance(record.observation, dict):
            data = record.observation
            await sync_browser_session(
                context,
                data,
                # ``_execution_id`` marks this as already-recorded (the router did it) so the
                # low-level INT-5 path in sync_browser_session does not mint a duplicate.
                event={
                    "type": "page",
                    "action": args.intent,
                    "url": data.get("url"),
                    "_execution_id": record.execution_id,
                },
            )
            return ToolResult(data=data)
        # blocked / failed → a recoverable error block the model can react to.
        return ToolResult(
            data={"error": record.error or f"intent {args.intent!r} did not complete ({record.status})"}
        )

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_intent_tool = BrowserIntentTool()
