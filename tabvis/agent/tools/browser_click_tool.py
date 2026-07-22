"""BrowserClick — click an element by ref, returning a fresh page snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tabvis.constants.tools import BROWSER_CLICK_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)
from tabvis.types.permissions import PermissionDecision

_DESCRIPTION = """Click an element ref or a viewport coordinate, then return a fresh snapshot of
the resulting page.

Usage:
 - ref must come from the MOST RECENT snapshot (e.g. 'e7'). If the page changed since, the tool
   returns a 'stale ref' error — call BrowserSnapshot to get fresh refs and try again.
 - For a canvas or visual-only page, omit ref and provide both coordinate_x and coordinate_y from
   the latest screenshot. Coordinates are CSS pixels within the current viewport.
 - Set double=true for a double-click.
 - Provide description (a short human label like "the blue Sign in button") for the transcript."""


class BrowserClickInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str | None = Field(
        default=None, description="Element ref from the latest snapshot, e.g. 'e7'."
    )
    coordinate_x: float | None = Field(
        default=None, ge=0, description="Viewport x coordinate for a visual-only target."
    )
    coordinate_y: float | None = Field(
        default=None, ge=0, description="Viewport y coordinate for a visual-only target."
    )
    double: bool = Field(default=False, description="Double-click instead of single-click.")
    description: str | None = Field(
        default=None, description="Short human label of the element being clicked."
    )

    @model_validator(mode="after")
    def _one_target(self) -> "BrowserClickInput":
        has_coordinates = self.coordinate_x is not None or self.coordinate_y is not None
        if bool(self.ref) == has_coordinates:
            raise ValueError("provide either ref or coordinate_x/coordinate_y")
        if has_coordinates and (self.coordinate_x is None or self.coordinate_y is None):
            raise ValueError("coordinate_x and coordinate_y must be provided together")
        return self


class BrowserClickTool(Tool):
    name = BROWSER_CLICK_TOOL_NAME
    search_hint = "browser: click a button, link, or element on the page"
    input_schema = BrowserClickInput
    # The browser is this agent's PRIMARY tool — always loaded, never deferred
    # behind ToolSearch. (WebFetch was removed; this is how the agent reaches the web.)
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
        label = (
            input.get("description")
            if isinstance(input, dict)
            else getattr(input, "description", None)
        )
        return f"Click {label}" if label else "Click an element in the browser"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserClick"

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
        args: BrowserClickInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.click(
                args.ref,
                double=args.double,
                coordinate_x=args.coordinate_x,
                coordinate_y=args.coordinate_y,
            )
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Click failed: {e}"})
        await sync_browser_session(
            context,
            data,
            event={
                "type": "interaction",
                "action": "double_click" if args.double else "click",
                "interaction": {
                    "ref": args.ref,
                    "coordinate_x": args.coordinate_x,
                    "coordinate_y": args.coordinate_y,
                    "double": args.double,
                },
            },
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_click_tool = BrowserClickTool()
