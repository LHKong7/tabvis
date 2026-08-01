from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from tabvis.agent.api.model_client import Options, select_tools_for_model_request
from tabvis.agent.tools.file_read_tool import FileReadInput, file_read_tool
from tabvis.agent.tools.tool_search_tool import (
    ToolSearchInput,
    get_prompt,
    tool_search_tool,
)
from tabvis.tool import Tool, ToolResult, ToolUseContext, ToolUseContextOptions
from tabvis.utils.messages import create_user_message
from tabvis.utils.tool_search import extract_discovered_tool_names


class _Input(BaseModel):
    value: str = ""


class _FakeTool(Tool):
    input_schema = _Input
    max_result_size_chars = 1_000

    def __init__(
        self,
        name: str,
        *,
        should_defer: bool = False,
        is_mcp: bool = False,
        always_load: bool = False,
    ) -> None:
        self.name = name
        self.should_defer = should_defer
        self.is_mcp = is_mcp
        self.always_load = always_load

    async def prompt(self, options: dict[str, Any]) -> str:
        return f"FULL SECRET SCHEMA DESCRIPTION FOR {self.name}"

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return await self.prompt(options)

    async def call(self, *args: Any, **kwargs: Any) -> ToolResult[str]:
        return ToolResult(data="ok")

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": str(content),
        }


def _options(model: str = "third-party-haiku") -> Options:
    return Options(model=model, query_source="sdk")


def test_initial_request_withholds_deferred_schemas_for_any_provider(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("TABVIS_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    monkeypatch.setenv("TABVIS_BASE_URL", "https://third-party.example/v1")
    core = _FakeTool("Core")
    optional = _FakeTool("Optional", should_defer=True)
    mcp = _FakeTool("mcp__mail__send", is_mcp=True)
    pinned_mcp = _FakeTool("mcp__policy__check", is_mcp=True, always_load=True)
    tools = [core, optional, mcp, pinned_mcp, tool_search_tool]

    selected = asyncio.run(select_tools_for_model_request([], tools, _options()))

    assert [tool.name for tool in selected] == [
        "Core",
        "mcp__policy__check",
        "ToolSearch",
    ]


def test_tool_search_result_loads_only_matched_schemas_on_next_turn(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("TABVIS_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    core = _FakeTool("Core")
    optional = _FakeTool("Optional", should_defer=True)
    other = _FakeTool("Other", should_defer=True)
    tools = [core, optional, other, tool_search_tool]
    messages = [
        create_user_message(
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "search-1",
                    "content": "Loaded deferred tool schemas for the next turn: Optional.",
                }
            ],
            tool_use_result={
                "type": "tool_search_result",
                "matches": ["Optional"],
                "query": "select:Optional",
                "total_deferred_tools": 2,
            },
        )
    ]

    selected = asyncio.run(
        select_tools_for_model_request(messages, tools, _options("gpt-compatible"))
    )

    assert [tool.name for tool in selected] == ["Core", "Optional", "ToolSearch"]
    assert extract_discovered_tool_names(messages) == {"Optional"}


def test_real_tool_search_result_drives_next_request_selection(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("TABVIS_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    core = _FakeTool("Core")
    optional = _FakeTool("Optional", should_defer=True)
    other = _FakeTool("Other", should_defer=True)
    tools = [core, optional, other, tool_search_tool]
    context = ToolUseContext(options=ToolUseContextOptions(tools=tools))
    result = asyncio.run(
        tool_search_tool.call(
            ToolSearchInput(query="select:Optional"),
            context,
            None,
            None,
        )
    )
    block = tool_search_tool.map_tool_result_to_tool_result_block_param(
        result.data, "search-1"
    )
    messages = [
        create_user_message(
            content=[block],
            tool_use_result=result.data,
        )
    ]

    selected = asyncio.run(
        select_tools_for_model_request(messages, tools, _options("gemini-compatible"))
    )

    assert result.data["type"] == "tool_search_result"
    assert result.data["matches"] == ["Optional"]
    assert [tool.name for tool in selected] == ["Core", "Optional", "ToolSearch"]


def test_discovered_schemas_survive_compaction_boundary(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("TABVIS_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    core = _FakeTool("Core")
    optional = _FakeTool("Optional", should_defer=True)
    tools = [core, optional, tool_search_tool]
    messages = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "compactMetadata": {"preCompactDiscoveredTools": ["Optional"]},
        }
    ]

    selected = asyncio.run(select_tools_for_model_request(messages, tools, _options()))

    assert [tool.name for tool in selected] == ["Core", "Optional", "ToolSearch"]


def test_disabled_mode_loads_all_schemas(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    monkeypatch.delenv("TABVIS_DISABLE_EXPERIMENTAL_BETAS", raising=False)
    tools = [
        _FakeTool("Core"),
        _FakeTool("Optional", should_defer=True),
        _FakeTool("mcp__mail__send", is_mcp=True),
        tool_search_tool,
    ]

    selected = asyncio.run(select_tools_for_model_request([], tools, _options()))

    assert selected == tools


def test_tool_search_prompt_discloses_names_not_full_schemas() -> None:
    tools = [
        _FakeTool("Core"),
        _FakeTool("Optional", should_defer=True),
        _FakeTool("mcp__mail__send", is_mcp=True),
        tool_search_tool,
    ]

    prompt = get_prompt(tools)

    assert "Optional" in prompt
    assert "mcp__mail__send" in prompt
    assert "Core" not in prompt
    assert "FULL SECRET SCHEMA DESCRIPTION" not in prompt


def test_tool_search_wire_result_is_provider_neutral_text() -> None:
    block = tool_search_tool.map_tool_result_to_tool_result_block_param(
        {
            "type": "tool_search_result",
            "matches": ["Optional", "mcp__mail__send"],
            "query": "send",
            "total_deferred_tools": 2,
        },
        "search-1",
    )

    assert block["type"] == "tool_result"
    assert isinstance(block["content"], str)
    assert "Optional" in block["content"]
    assert "tool_reference" not in block["content"]


def test_infrequent_workflow_tool_is_deferred() -> None:
    from tabvis.agent.tools.workflow_tool import workflow_tool

    assert workflow_tool.should_defer is True


def test_read_defaults_to_bounded_window_with_explicit_continuation(tmp_path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("\n".join(f"line-{number}" for number in range(1, 2_006)))
    context = ToolUseContext()

    first = asyncio.run(
        file_read_tool.call(FileReadInput(file_path=str(path)), context)
    )
    first_file = first.data["file"]

    assert first_file["startLine"] == 1
    assert first_file["endLine"] == 2_000
    assert first_file["totalLines"] == 2_005
    assert first_file["hasMore"] is True
    assert first_file["nextOffset"] == 2_001
    block = file_read_tool.map_tool_result_to_tool_result_block_param(
        first.data, "read-1"
    )
    assert "Showing lines 1-2000 of 2005" in block["content"]
    assert "offset=2001 limit=2000" in block["content"]

    final = asyncio.run(
        file_read_tool.call(
            FileReadInput(file_path=str(path), offset=2_001, limit=2_000),
            context,
        )
    )
    assert final.data["file"]["numLines"] == 5
    assert final.data["file"]["hasMore"] is False


def test_read_handles_a_single_oversized_line_without_unbounded_output(tmp_path) -> None:
    path = tmp_path / "one-huge-line.txt"
    path.write_text("x" * 1_000)
    context = ToolUseContext(
        file_reading_limits={"maxSizeBytes": 100, "maxTokens": 100}
    )

    result = asyncio.run(
        file_read_tool.call(FileReadInput(file_path=str(path)), context)
    )
    block = file_read_tool.map_tool_result_to_tool_result_block_param(
        result.data, "read-large-line"
    )

    assert result.data["file"]["content"] == ""
    assert result.data["file"]["truncatedByBytes"] is True
    assert "larger than the bounded Read output window" in block["content"]
