"""Session transcripts survive both normal completion and interrupted Web runs."""

from __future__ import annotations

import asyncio
from typing import Any

from tabvis.agent.query import QueryDeps
from tabvis.agent import query_engine
from tabvis.tool import ToolUseContext, ToolUseContextOptions
from tabvis.utils.messages import create_assistant_message


async def _allow_tool(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {"behavior": "allow"}


def _context() -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(tools=[], main_loop_model="test-model"),
        messages=[],
    )


def test_completed_turn_persists_exactly_once(monkeypatch) -> None:
    persisted: list[list[dict[str, Any]]] = []

    async def persist(messages: list[dict[str, Any]]) -> None:
        persisted.append(list(messages))

    async def call_model(**_kwargs: Any):
        yield create_assistant_message(content="done")

    monkeypatch.setattr(query_engine, "_persist_session_transcript", persist)

    async def scenario() -> None:
        results = [
            item
            async for item in query_engine.ask(
                prompt="hello",
                tools=[],
                app_state_store=None,
                can_use_tool=_allow_tool,
                session_id="sid-completed",
                model="test-model",
                system_prompt=[],
                context=_context(),
                deps=QueryDeps(call_model=call_model),
            )
        ]
        assert results[-1]["type"] == "result"

    asyncio.run(scenario())

    assert len(persisted) == 1
    assert [message["type"] for message in persisted[0]] == ["user", "assistant"]


def test_closed_turn_persists_messages_seen_before_cancellation(monkeypatch) -> None:
    persisted: list[list[dict[str, Any]]] = []

    async def persist(messages: list[dict[str, Any]]) -> None:
        persisted.append(list(messages))

    async def call_model(**_kwargs: Any):
        yield create_assistant_message(content="research evidence collected so far")

    monkeypatch.setattr(query_engine, "_persist_session_transcript", persist)

    async def scenario() -> None:
        stream = query_engine.ask(
            prompt="research this topic",
            tools=[],
            app_state_store=None,
            can_use_tool=_allow_tool,
            session_id="sid-cancelled",
            model="test-model",
            system_prompt=[],
            context=_context(),
            deps=QueryDeps(call_model=call_model),
        )
        assert (await anext(stream))["type"] == "system"
        assert (await anext(stream))["type"] == "assistant"
        await stream.aclose()

    asyncio.run(scenario())

    assert len(persisted) == 1
    assert [message["type"] for message in persisted[0]] == ["user", "assistant"]
    assert (
        persisted[0][-1]["message"]["content"][0]["text"]
        == "research evidence collected so far"
    )
