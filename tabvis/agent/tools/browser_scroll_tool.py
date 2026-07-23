"""BrowserScroll — scroll the page or an element container and return a fresh snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.constants.tools import BROWSER_SCROLL_TOOL_NAME
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision

_DESCRIPTION = """Scroll the current page or a scrollable element, then return a fresh page
snapshot with new refs.

Usage:
 - down=true scrolls toward later content; down=false scrolls toward earlier content.
 - pages is measured in viewport heights and may be fractional. Multi-page requests are delivered
   one page at a time so lazy-loaded content can react between steps.
 - Set ref to scroll a specific container. Omit ref to scroll the main page.
 - Read the returned snapshot before acting because scrolling rebuilds the current ref map."""


class BrowserScrollInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    down: bool = Field(default=True, description="Scroll down when true; up when false.")
    pages: float = Field(
        default=1.0, gt=0, le=100, description="Distance in viewport pages (fractional allowed)."
    )
    ref: str | None = Field(
        default=None, description="Optional ref for a scrollable element container."
    )
    description: str | None = Field(
        default=None, description="Short human label for the content being scrolled."
    )


class BrowserScrollTool(Tool):
    name = BROWSER_SCROLL_TOOL_NAME
    search_hint = "browser: scroll page, scroll element, reveal more content"
    input_schema = BrowserScrollInput
    should_defer = False
    always_load = True
    max_result_size_chars = 48_000

    def is_enabled(self) -> bool:
        return playwright_available()

    def is_read_only(self, input: Any) -> bool:
        return False

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        label = input.get("description") if isinstance(input, dict) else input.description
        direction = "down" if (input.get("down", True) if isinstance(input, dict) else input.down) else "up"
        return f"Scroll {label} {direction}" if label else f"Scroll the browser {direction}"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserScroll"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def call(
        self,
        args: BrowserScrollInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.scroll(down=args.down, pages=args.pages, ref=args.ref)
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Scroll failed: {e}"})
        await sync_browser_session(
            context,
            data,
            event={
                "type": "interaction",
                "action": "scroll",
                "interaction": {"ref": args.ref, "down": args.down, "pages": args.pages},
            },
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_scroll_tool = BrowserScrollTool()
