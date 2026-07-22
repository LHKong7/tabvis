"""BrowserKeys — send a special key or shortcut and return a fresh snapshot."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tabvis.agent.tools.browser_common import (
    observation_to_block,
    playwright_available,
    sync_browser_session,
)
from tabvis.browser.browser_service import BrowserError, _normalize_key_sequence
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.constants.tools import BROWSER_KEYS_TOOL_NAME
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision

_DESCRIPTION = """Send a special key or keyboard shortcut to the current page, then return a
fresh snapshot.

Supported examples include Enter, Tab, Escape, PageUp, PageDown, ArrowUp, ArrowDown, Control+A,
Meta+A, and Shift+Tab. Set ref to focus a particular element before sending the keys; otherwise the
page's current focus is used."""


class BrowserKeysInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: str = Field(min_length=1, description="Special key or '+'-joined shortcut.")
    ref: str | None = Field(
        default=None, description="Optional fresh ref to focus before sending the key sequence."
    )
    description: str | None = Field(
        default=None, description="Short human label for the keyboard operation."
    )

    @field_validator("keys")
    @classmethod
    def _valid_keys(cls, value: str) -> str:
        # Validate at the tool boundary, while the service repeats validation for direct callers.
        try:
            _normalize_key_sequence(value)
        except BrowserError as e:
            raise ValueError(str(e)) from e
        return value


class BrowserKeysTool(Tool):
    name = BROWSER_KEYS_TOOL_NAME
    search_hint = "browser: press Enter, Tab, Escape, arrow key, keyboard shortcut"
    input_schema = BrowserKeysInput
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
        keys = input.get("keys") if isinstance(input, dict) else input.keys
        label = input.get("description") if isinstance(input, dict) else input.description
        return f"Send {keys} to {label}" if label else f"Send {keys} in the browser"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserKeys"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def call(
        self,
        args: BrowserKeysInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.send_keys(args.keys, ref=args.ref)
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Send keys failed: {e}"})
        await sync_browser_session(
            context,
            data,
            event={
                "type": "interaction",
                "action": "keys",
                "interaction": {"ref": args.ref, "keys": args.keys},
            },
        )
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return observation_to_block(content if isinstance(content, dict) else {}, tool_use_id)


browser_keys_tool = BrowserKeysTool()
