"""BrowserSnapshot — observe the current page as a ref-tagged accessibility snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.constants.tools import BROWSER_SNAPSHOT_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)

_DESCRIPTION = """Capture the current browser page as an accessibility snapshot: a compact list
of interactive and named elements, each tagged with a stable [ref=eN] you pass to
BrowserClick / BrowserType.

Usage:
 - Call this first (after BrowserNavigate) to see the page before acting.
 - The act tools (BrowserClick, BrowserType) already return a fresh snapshot, so only call this
   to re-observe without acting, or to capture a screenshot.
 - Set include_screenshot=true when you need to visually verify layout; the image supplements
   the snapshot, it does not replace the refs (only the text snapshot carries refs you can act on).
   With a screenshot, each ref also carries its [box=x,y,w,h] so you can line it up with the image.
 - On a visual page the accessibility tree can't describe (a canvas app, a map, an image-only
   page), a screenshot and the page's raw HTML are added automatically — reason from those.
 - Only use refs from the most recent snapshot."""


class BrowserSnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_screenshot: bool = Field(
        default=False, description="Also return a PNG screenshot of the page as an image."
    )


class BrowserSnapshotTool(Tool):
    name = BROWSER_SNAPSHOT_TOOL_NAME
    search_hint = "browser: snapshot the page, see interactive elements, screenshot"
    input_schema = BrowserSnapshotInput
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

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return "Snapshot the current browser page"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserSnapshot"

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
        args: BrowserSnapshotInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.snapshot(include_screenshot=args.include_screenshot)
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Snapshot failed: {e}"})
        await sync_browser_session(
            context, data, event={"type": "page", "action": "snapshot"}
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_snapshot_tool = BrowserSnapshotTool()
