"""BrowserWait — let a page finish loading before acting on it.

Real pages are not ready when ``load`` fires: single-page apps render after it, content lazy-loads
on scroll, and interstitials ("Just a moment…", "Loading…", cookie walls) replace themselves a few
seconds later. Without a way to wait, the agent snapshots a half-built page and reasons about the
wrong thing — usually concluding a site is "broken" or "blocked" when it simply had not finished.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tabvis.constants.tools import BROWSER_WAIT_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)

_DESCRIPTION = """Wait for the current page to be ready, then return a fresh snapshot.

Use this when a page is clearly not finished: it shows a spinner, "Loading…", "Just a moment…",
an almost-empty snapshot, or content you expected is missing. Pages routinely keep rendering
after they "load" — single-page apps, lazy content, and interstitials that replace themselves.

Usage:
 - for_text: wait until this text appears on the page (the most reliable option — wait for
   something you actually expect to see, e.g. a product name or a heading).
 - for_gone: wait until this text DISAPPEARS (e.g. "Loading", "Just a moment").
 - load_state: 'networkidle' waits for network activity to stop — good for SPAs.
 - time_ms: a plain wait, as a last resort.
If the wait times out you get the page as it is, plus a note — decide from the snapshot whether
the page is genuinely stuck or simply slow. Do not spam this tool; if two waits don't help, the
page is not going to load, and you should tell the user what you see."""


class BrowserWaitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    for_text: str | None = Field(
        default=None, description="Wait until this text appears on the page."
    )
    for_gone: str | None = Field(
        default=None, description="Wait until this text disappears (e.g. 'Loading')."
    )
    load_state: Literal["load", "domcontentloaded", "networkidle"] | None = Field(
        default=None, description="Wait for this load state; 'networkidle' suits SPAs."
    )
    time_ms: int | None = Field(
        default=None, ge=1, le=60_000, description="Plain wait in ms (last resort)."
    )
    timeout_ms: int | None = Field(
        default=None, ge=1, le=120_000, description="Give up after this long. Default 15000."
    )

    @model_validator(mode="after")
    def _need_one(self) -> BrowserWaitInput:
        if not any((self.for_text, self.for_gone, self.load_state, self.time_ms)):
            raise ValueError(
                "give one of: for_text, for_gone, load_state, time_ms"
            )
        return self


class BrowserWaitTool(Tool):
    name = BROWSER_WAIT_TOOL_NAME
    search_hint = "browser: wait for a page to load, for text to appear, for a spinner to go"
    input_schema = BrowserWaitInput
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
        return "Wait for the browser page to be ready"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserWait"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        # IDP-8: route through the single Policy Guard (allow for non-navigation tools today).
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def call(
        self,
        args: BrowserWaitInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.wait_for(
                for_text=args.for_text,
                for_gone=args.for_gone,
                load_state=args.load_state,
                time_ms=args.time_ms,
                timeout_ms=args.timeout_ms,
            )
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Wait failed: {e}"})
        await sync_browser_session(
            context, data, event={"type": "page", "action": "wait"}
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_wait_tool = BrowserWaitTool()
