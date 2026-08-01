"""The query loop keeps permission checks attached to the live conversation."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from tabvis.agent.query import QueryDeps, QueryParams, Terminal, query
from tabvis.agent.tools.file_write_tool import file_write_tool
from tabvis.tool import ToolUseContext, ToolUseContextOptions
from tabvis.utils.messages import (
    create_assistant_api_error_message,
    create_assistant_message,
    create_user_message,
)


def test_read_only_prompt_reaches_write_permission_check(tmp_path) -> None:
    target = tmp_path / "read-only-probe.md"
    prompt = (
        "Do not modify or create files. To verify the protection, try Write once and stop if "
        "approval is required."
    )
    model_turn = 0
    decisions: list[dict[str, Any]] = []

    async def call_model(**kwargs: Any):
        nonlocal model_turn
        model_turn += 1
        if model_turn == 1:
            yield create_assistant_message(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tool_write_probe",
                        "name": "Write",
                        "input": {"file_path": str(target), "content": "probe"},
                    }
                ]
            )
        else:
            yield create_assistant_message(content="Stopped because approval is required.")

    async def can_use_tool(
        tool: Any,
        tool_input: Any,
        context: ToolUseContext,
        assistant_message: dict[str, Any],
        tool_use_id: str,
    ) -> dict[str, Any]:
        del assistant_message, tool_use_id
        decision = await tool.check_permissions(tool_input, context)
        decisions.append(decision)
        if decision.get("behavior") == "ask":
            return {
                "behavior": "deny",
                "message": decision.get("message", "Approval required."),
            }
        return decision

    async def scenario() -> None:
        context = ToolUseContext(
            options=ToolUseContextOptions(tools=[file_write_tool]),
            messages=[],
        )
        items = [
            item
            async for item in query(
                QueryParams(
                    messages=[create_user_message(content=prompt)],
                    system_prompt=[],
                    tools=[file_write_tool],
                    can_use_tool=can_use_tool,
                    tool_use_context=context,
                    deps=QueryDeps(call_model=call_model),
                )
            )
        ]

        assert isinstance(items[-1], Terminal)
        assert decisions[0]["behavior"] == "ask"
        assert decisions[0]["decisionReason"]["rule"] == "request-intent-read-only"
        assert context.messages[0]["message"]["content"] == prompt
        assert not target.exists()

    asyncio.run(scenario())


def test_model_timeout_after_tool_does_not_repeat_tool_or_model_turn(monkeypatch) -> None:
    """The timeout layer already retried once; the Agent loop must stop without replaying tools."""
    query_module = importlib.import_module("tabvis.agent.query")
    model_calls = 0
    tool_runs = 0

    async def call_model(**_kwargs: Any):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            yield create_assistant_message(
                content=[
                    {
                        "type": "tool_use",
                        "id": "tool_once",
                        "name": "BrowserClick",
                        "input": {"ref": "e1"},
                    }
                ]
            )
        else:
            yield create_assistant_api_error_message(
                content="Model response timed out.",
                api_error="model_stream_timeout",
                error="model_stream_timeout",
            )

    async def run_tools_once(*_args: Any, **_kwargs: Any):
        nonlocal tool_runs
        tool_runs += 1
        yield {
            "message": create_user_message(
                content=[
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool_once",
                        "content": "clicked",
                    }
                ]
            )
        }

    monkeypatch.setattr(query_module, "run_tools", run_tools_once)

    async def scenario() -> None:
        context = ToolUseContext(
            options=ToolUseContextOptions(tools=[]),
            messages=[],
        )
        items = [
            item
            async for item in query(
                QueryParams(
                    messages=[create_user_message(content="click once")],
                    system_prompt=[],
                    tools=[],
                    can_use_tool=lambda *_a, **_kw: None,
                    tool_use_context=context,
                    deps=QueryDeps(call_model=call_model),
                )
            )
        ]
        assert isinstance(items[-1], Terminal)

    asyncio.run(scenario())
    assert model_calls == 2
    assert tool_runs == 1
