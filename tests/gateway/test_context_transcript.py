"""Transcript mapping for the Context Runtime (design §11.3 #4)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.runtime.context import transcript as tx
from tabvis.gateway.runtime.context.transcript import _extract_text, map_transcript_messages


# --- pure mapping ------------------------------------------------------------------------------


def test_extract_text_handles_string_and_blocks() -> None:
    assert _extract_text("hello") == "hello"
    assert _extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a b"
    assert _extract_text([{"type": "tool_use", "name": "click"}]) == "[tool_use: click]"
    assert _extract_text([{"type": "tool_result"}, {"type": "image"}]) == "[tool_result] [image]"
    assert _extract_text(None) == "" and _extract_text(42) == ""


def test_map_drops_progress_and_flattens_content() -> None:
    chain = [
        {"type": "user", "uuid": "u1", "timestamp": "t1", "message": {"role": "user", "content": "hello"}},
        {"type": "progress", "uuid": "p1"},  # not a transcript message → dropped
        {"type": "assistant", "uuid": "a1", "timestamp": "t2",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "click"}]}},
        {"type": "attachment", "uuid": "at1", "message": {"content": []}},
    ]
    mapped = map_transcript_messages(chain)
    assert [m["id"] for m in mapped] == ["u1", "a1", "at1"]  # progress gone, order preserved
    assert mapped[0] == {"id": "u1", "role": "user", "text": "hello", "ts": "t1"}
    assert mapped[1]["text"] == "hi [tool_use: click]" and mapped[1]["role"] == "assistant"
    assert mapped[2]["role"] == "attachment" and mapped[2]["text"] == "[attachment]"  # attachment fallback


def test_map_role_falls_back_to_entry_type() -> None:
    chain = [{"type": "system", "uuid": "s1", "message": {"content": "note"}}]  # no message.role
    assert map_transcript_messages(chain)[0]["role"] == "system"


def test_map_empty_chain() -> None:
    assert map_transcript_messages([]) == []


# --- real loader (real build_conversation_chain, patched file IO) -------------------------------


def test_load_session_transcript_builds_and_maps_the_chain(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import tabvis.utils.session_storage as ss

    path = tmp_path / "session.jsonl"
    path.write_text("{}", encoding="utf-8")  # content irrelevant — load_transcript_file is faked
    messages = {
        "u1": {"type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-01-01T00:00:00",
               "message": {"role": "user", "content": "q"}},
        "a1": {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-01-01T00:00:01",
               "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}]}},
    }

    async def fake_load(file_path, opts=None):
        return {"messages": messages, "leafUuids": ["a1"]}

    monkeypatch.setattr(ss, "get_transcript_path_for_session", lambda sid: str(path))
    monkeypatch.setattr(ss, "load_transcript_file", fake_load)
    # build_conversation_chain and is_transcript_message stay REAL.

    mapped = asyncio.run(tx.load_session_transcript("ses_1"))
    assert [m["id"] for m in mapped] == ["u1", "a1"]     # leaf→root chain, reversed to root→leaf
    assert mapped[0]["text"] == "q" and mapped[1]["text"] == "answer"


def test_load_session_transcript_missing_file_is_empty(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import tabvis.utils.session_storage as ss

    monkeypatch.setattr(ss, "get_transcript_path_for_session", lambda sid: str(tmp_path / "nope.jsonl"))
    assert asyncio.run(tx.load_session_transcript("ses_missing")) == []


def test_source_collector_uses_the_real_transcript_loader_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no injected hook, the collector calls load_session_transcript; a fresh session → no source.
    from tabvis.gateway.runtime.context.sources import SourceCollector

    async def scenario() -> None:
        async def none(*a):
            return None

        req = await SourceCollector(project_instructions=none, memory=none, git_status=none).collect(
            run_id="run_1", session_id="ses_fresh", model="m"
        )
        assert "transcript" not in req.sources  # no transcript file for a fresh session → omitted

    asyncio.run(scenario())
