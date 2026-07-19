"""BrowserNavigate — open a URL or move through history, returning a fresh page snapshot."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tabvis.constants.tools import BROWSER_NAVIGATE_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)
from tabvis.types.permissions import PermissionDecision

_DESCRIPTION = """Drive the browser: open a URL, or go back / forward / reload.

Returns an accessibility snapshot of the resulting page — a compact list of interactive and
named elements, each tagged with a stable [ref=eN] you pass to BrowserClick / BrowserType.

Usage:
 - action='goto' requires a fully-formed url (http/https). Other actions ignore url.
 - The returned snapshot already reflects the new page, so you usually do not need a separate
   BrowserSnapshot after navigating — read the refs and act.
 - Only use refs from the MOST RECENT snapshot; navigating invalidates older refs.
 - If a navigation is blocked by the domain allowlist, tell the user which domain to add to
   their settings rather than retrying the same navigation."""


class BrowserNavigateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(
        default=None, description="URL to open (required when action is 'goto')."
    )
    action: Literal["goto", "back", "forward", "reload"] = Field(
        default="goto", description="Navigation action."
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="load", description="When to consider the navigation finished."
    )

    @model_validator(mode="after")
    def _require_url_for_goto(self) -> BrowserNavigateInput:
        if self.action == "goto" and not (self.url or "").strip():
            raise ValueError("url is required when action is 'goto'")
        return self


class BrowserNavigateTool(Tool):
    name = BROWSER_NAVIGATE_TOOL_NAME
    search_hint = "browser: open a url, navigate a web page, go back or forward"
    input_schema = BrowserNavigateInput
    # The browser is this agent's PRIMARY tool — always loaded, never deferred
    # behind ToolSearch. (WebFetch was removed; this is how the agent reaches the web.)
    should_defer = False
    always_load = True
    max_result_size_chars = 48_000

    def is_enabled(self) -> bool:
        return playwright_available()

    def is_read_only(self, input: Any) -> bool:
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    def interrupt_behavior(self) -> str:
        return "cancel"

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        url = input.get("url") if isinstance(input, dict) else getattr(input, "url", None)
        return f"Navigate the browser to {url}" if url else "Navigate the browser"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserNavigate"

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        # IDP-8: the single Policy Guard entry — for BrowserNavigate it applies the domain allowlist.
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def call(
        self,
        args: BrowserNavigateInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.navigate(
                args.url or "", action=args.action, wait_until=args.wait_until
            )
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Navigation failed: {e}"})
        await sync_browser_session(
            context,
            data,
            event={"type": "navigation", "action": args.action, "url": args.url},
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_navigate_tool = BrowserNavigateTool()
