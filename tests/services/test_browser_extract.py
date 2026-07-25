"""Structured rendered-page extraction and tool wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tabvis.agent.tools.browser_extract_tool import BrowserExtractInput, browser_extract_tool
from tabvis.browser.browser_service import BrowserService
from tabvis.browser.policy_guard import evaluate
from tabvis.constants.tools import BROWSER_EXTRACT_TOOL_NAME


class _Page:
    url = "https://example.test/investors"

    def is_closed(self) -> bool:
        return False

    async def title(self) -> str:
        return "Investor results"

    async def evaluate(self, script: str, args: dict) -> dict:
        assert "querySelectorAll('a[href]')" in script
        assert args["query"] == "2026"
        return {
            "scope": "main/article/body",
            "query": "2026",
            "text": "March Quarter 2026 and Full Fiscal Year 2026 Results",
            "matches": ["March Quarter 2026 and Full Fiscal Year 2026 Results"],
            "headings": [{"level": 2, "text": "Quarterly Results"}],
            "dates": ["2026-05-13"],
            "links": [
                {
                    "text": "Press Releases",
                    "href": "https://example.test/document-1991237455038119936",
                    "title": "",
                    "target": "_blank",
                    "kind": "page",
                    "context": "March Quarter 2026",
                }
            ],
            "tables": [],
        }


def test_extract_page_returns_structured_absolute_links() -> None:
    service = BrowserService()
    page = _Page()
    service._context = SimpleNamespace(pages=[page])  # type: ignore[assignment]
    service._active_page = page  # type: ignore[assignment]
    data = asyncio.run(service.extract_page(query="2026"))
    assert data["url"] == page.url
    assert data["dates"] == ["2026-05-13"]
    assert data["links"][0]["href"].endswith("1991237455038119936")


def test_browser_extract_is_registered_and_read_only() -> None:
    from tabvis.agent.tools import get_all_base_tools

    assert BROWSER_EXTRACT_TOOL_NAME in {tool.name for tool in get_all_base_tools()}
    assert browser_extract_tool.is_read_only(BrowserExtractInput()) is True
    assert evaluate(BROWSER_EXTRACT_TOOL_NAME, {}, None)["behavior"] == "allow"


def test_browser_extract_mapper_keeps_href_as_structured_json() -> None:
    url = "https://example.test/document-1991237455038119936"
    block = browser_extract_tool.map_tool_result_to_tool_result_block_param(
        {"url": "https://example.test", "links": [{"text": "report", "href": url}]},
        "tu_1",
    )
    assert url in block["content"]
