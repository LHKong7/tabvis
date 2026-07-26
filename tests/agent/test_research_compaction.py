"""Durable browser research evidence survives compaction."""

from __future__ import annotations

import asyncio
from typing import Any

from tabvis.agent.compact.prompt import get_compact_prompt
from tabvis.agent.query import (
    QueryDeps,
    QueryParams,
    Terminal,
    _restore_research_evidence_after_compaction,
    query,
)
from tabvis.bootstrap.state import switch_session
from tabvis.browser.artifacts import record_browser_artifact
from tabvis.tool import ToolUseContext, ToolUseContextOptions
from tabvis.utils.messages import create_assistant_message
from tabvis.utils.messages import create_user_message


def test_compact_prompt_preserves_exact_research_provenance() -> None:
    prompt = get_compact_prompt()
    assert "exact research source URLs" in prompt
    assert "exact page ranges already read" in prompt
    assert "full text, OCR, or abstract-only" in prompt
    assert "numeric findings" in prompt
    assert "requested report path" in prompt
    assert "last evidence already written" in prompt


def test_post_compact_messages_restore_durable_research_evidence(monkeypatch) -> None:
    session_id = "sess-research-compact"
    switch_session(session_id)
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    asyncio.run(
        record_browser_artifact(
            {"type": "page", "action": "extract"},
            {
                "url": "https://example.test/audit",
                "title": "Audit benchmark",
                "query": "2026",
                "dates": ["2026-07-26"],
                "text": "Detection 37.5%; patching 23.4%.",
                "headings": [],
                "links": [],
                "tables": [],
            },
        )
    )

    compacted = [create_user_message(content="Summary from the compaction model.")]
    restored = _restore_research_evidence_after_compaction(compacted)

    assert len(restored) == 2
    evidence_message = restored[-1]
    assert evidence_message["isMeta"] is True
    content = evidence_message["message"]["content"]
    assert "https://example.test/audit" in content
    assert "Detection 37.5%; patching 23.4%." in content
    assert "LOW-PRIVILEGE EXTERNAL DATA" in content


def test_query_reinjects_evidence_when_auto_compact_fires(monkeypatch) -> None:
    session_id = "sess-query-research-compact"
    switch_session(session_id)
    monkeypatch.setenv("TABVIS_BROWSER_ARTIFACTS_DOM", "0")
    asyncio.run(
        record_browser_artifact(
            {"type": "page", "action": "extract"},
            {
                "url": "https://example.test/source",
                "title": "Primary source",
                "query": None,
                "dates": ["2026-07-26"],
                "text": "Exact finding survives compact.",
                "headings": [],
                "links": [],
                "tables": [],
            },
        )
    )

    async def compact_once(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"wasCompacted": True, "compactionResult": {"fake": True}}

    def build_messages(_result: dict[str, Any]) -> list[dict[str, Any]]:
        return [create_user_message(content="Lossy summary.")]

    seen_by_model: list[dict[str, Any]] = []

    async def call_model(**kwargs: Any):
        seen_by_model.extend(kwargs["messages"])
        yield create_assistant_message(content="done")

    monkeypatch.setattr(
        "tabvis.agent.compact.auto_compact.auto_compact_if_needed",
        compact_once,
    )
    monkeypatch.setattr(
        "tabvis.agent.compact.compact.build_post_compact_messages",
        build_messages,
    )

    async def allow(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"behavior": "allow"}

    async def scenario() -> None:
        context = ToolUseContext(
            options=ToolUseContextOptions(tools=[], main_loop_model="test-model"),
            messages=[],
        )
        items = [
            item
            async for item in query(
                QueryParams(
                    messages=[create_user_message(content="Research and report.")],
                    system_prompt=[],
                    tools=[],
                    can_use_tool=allow,
                    tool_use_context=context,
                    deps=QueryDeps(call_model=call_model),
                )
            )
        ]
        assert isinstance(items[-1], Terminal)

    asyncio.run(scenario())

    rendered = str(seen_by_model)
    assert "Lossy summary." in rendered
    assert "https://example.test/source" in rendered
    assert "Exact finding survives compact." in rendered
