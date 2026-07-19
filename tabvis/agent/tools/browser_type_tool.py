"""BrowserType — type text into an element by ref, returning a fresh page snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.constants.tools import BROWSER_TYPE_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision
from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)

_DESCRIPTION = """Type text into the element identified by ref (from the latest snapshot), then
return a fresh snapshot of the resulting page.

Usage:
 - ref must come from the MOST RECENT snapshot (e.g. 'e8'). A 'stale ref' error means the page
   changed — call BrowserSnapshot for fresh refs.
 - clear=true (default) replaces the field's contents; clear=false appends to what's there.
 - submit=true presses Enter after typing (use to submit a search box or login form)."""


class BrowserTypeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(description="Element ref from the latest snapshot, e.g. 'e8'.")
    text: str = Field(description="Text to type into the element.")
    clear: bool = Field(default=True, description="Clear the field before typing.")
    submit: bool = Field(default=False, description="Press Enter after typing.")
    description: str | None = Field(
        default=None, description="Short human label of the field being filled."
    )


class BrowserTypeTool(Tool):
    name = BROWSER_TYPE_TOOL_NAME
    search_hint = "browser: type text, fill a form field, enter a search query"
    input_schema = BrowserTypeInput
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
        return f"Type into {label}" if label else "Type text in the browser"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserType"

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
        args: BrowserTypeInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.type_text(
                args.ref, args.text, clear=args.clear, submit=args.submit
            )
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Type failed: {e}"})
        await sync_browser_session(
            context,
            data,
            event={
                "type": "interaction",
                "action": "type",
                "interaction": {
                    "ref": args.ref,
                    "text": args.text,
                    "clear": args.clear,
                    "submit": args.submit,
                },
            },
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_type_tool = BrowserTypeTool()
