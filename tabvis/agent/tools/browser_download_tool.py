"""BrowserDownload — fetch a URL through the browser (cookies/auth apply) into the workspace."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.constants.tools import BROWSER_DOWNLOAD_TOOL_NAME
from tabvis.browser.browser_service import BrowserError
from tabvis.browser.manager import get_or_create_browser_service
from tabvis.agent.tools.browser_common import playwright_available
from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.permissions import PermissionDecision

_DESCRIPTION = """Download a file (PDF, CSV, zip, image, …) into the local download workspace, then
Read it to evaluate its contents.

Use this when you encounter a link or URL to a downloadable file — especially a PDF, which Chromium
would otherwise only render in its viewer (unreadable). The file is fetched through the browser
session, so cookies and logins apply.

Usage:
 - Pass the direct file `url`. The saved path is returned; then call the Read tool on that path.
 - Ordinary browser downloads (a click that triggers a download) and navigating to a PDF are ALSO
   captured to the workspace automatically — you only need this tool for an explicit URL."""


class BrowserDownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Direct URL of the file to download (http/https).")
    filename: str | None = Field(
        default=None, description="Optional filename to save as (else derived from the URL)."
    )


class BrowserDownloadTool(Tool):
    name = BROWSER_DOWNLOAD_TOOL_NAME
    search_hint = "browser: download a file / PDF / link to the workspace to read it"
    input_schema = BrowserDownloadInput
    should_defer = False
    always_load = True

    def is_enabled(self) -> bool:
        return playwright_available()

    def is_read_only(self, input: Any) -> bool:
        return False  # writes a file to the workspace

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        url = input.get("url") if isinstance(input, dict) else getattr(input, "url", None)
        return f"Download {url} to the workspace" if url else "Download a file to the workspace"

    def user_facing_name(self, input: Any | None = None) -> str:
        return "BrowserDownload"

    async def prompt(self, options: dict[str, Any]) -> str:
        return _DESCRIPTION

    async def check_permissions(self, input: Any, context: ToolUseContext) -> PermissionDecision:
        from tabvis.browser.policy_guard import evaluate

        return evaluate(self.name, input, context)

    async def call(
        self,
        args: BrowserDownloadInput,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        try:
            service = await get_or_create_browser_service()
            data = await service.download(args.url, filename=args.filename)
        except BrowserError as e:
            return ToolResult(data={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - surface as a recoverable tool error
            return ToolResult(data={"error": f"Download failed: {e}"})
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        if data.get("error"):
            text = f"Download failed: {data['error']}"
        else:
            dl = data.get("downloaded") or {}
            text = (
                f"Saved to the download workspace:\n  {dl.get('path')}\n"
                f"Use the Read tool on that path to evaluate the file."
            )
        return {"tool_use_id": tool_use_id, "type": "tool_result", "content": text}


browser_download_tool = BrowserDownloadTool()
