"""Tests for session RESUME — ``session_storage.load_conversation_for_resume``.

This is the read side that lets a reused agent see its earlier conversation: it reconstructs the
ordered user/assistant message envelopes from the on-disk transcript so they can be re-seeded into
the model. We write a small ``.jsonl`` transcript by hand (the parentUuid chain the writer would
have produced) and assert the reconstruction. ``config_home`` (autouse) roots the transcript dir in
a tmp dir.
"""

from __future__ import annotations

import asyncio
import json
import os

from tabvis.agent.run_context import RunContext, run_context_scope
from tabvis.bootstrap.state import (
    get_session_id,
    get_session_project_dir,
    switch_session,
)
from tabvis.utils import session_storage as ss
from tabvis.utils.messages import create_user_message


def _session_file(session_id: str) -> str:
    directory = ss.get_session_project_dir() or ss.get_project_dir(ss.get_original_cwd())
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"{session_id}.jsonl")


def _env(uuid: str, parent: str | None, mtype: str, role: str, text: str, ts: str) -> dict:
    return {
        "type": mtype,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": ts,
        "sessionId": "sid-resume",
        "message": {"role": role, "content": [{"type": "text", "text": text}]},
    }


def _write(session_id: str, envelopes: list[dict]) -> None:
    with open(_session_file(session_id), "w", encoding="utf-8") as fh:
        for e in envelopes:
            fh.write(json.dumps(e) + "\n")


def test_resume_loads_prior_conversation_in_order() -> None:
    sid = "sid-resume"
    _write(
        sid,
        [
            _env("u1", None, "user", "user", "hello", "2024-01-01T00:00:01Z"),
            _env("a1", "u1", "assistant", "assistant", "hi there", "2024-01-01T00:00:02Z"),
            _env("u2", "a1", "user", "user", "and then?", "2024-01-01T00:00:03Z"),
        ],
    )
    convo = asyncio.run(ss.load_conversation_for_resume(sid))

    assert [m["uuid"] for m in convo] == ["u1", "a1", "u2"]          # root → leaf order
    assert [m["message"]["role"] for m in convo] == ["user", "assistant", "user"]
    assert convo[0]["message"]["content"][0]["text"] == "hello"
    # remove_extra_fields strips parentUuid; the model-relevant fields survive.
    assert all("parentUuid" not in m for m in convo)
    assert all("message" in m and "type" in m for m in convo)


def test_resume_missing_session_returns_empty() -> None:
    """A brand-new session has no transcript — resume degrades to [] so callers can prepend freely."""
    assert asyncio.run(ss.load_conversation_for_resume("no-such-session-xyz")) == []


def test_sequential_runs_write_and_resume_their_own_transcripts(
    monkeypatch,
) -> None:
    """A process-wide Project must not keep writing to the preceding Run's session file."""
    monkeypatch.setenv("TEST_ENABLE_SESSION_PERSISTENCE", "1")
    original_session = get_session_id()
    original_project_dir = get_session_project_dir()
    cwd = ss.get_original_cwd()
    project_dir = ss.get_project_dir(cwd)
    first_session = "sid-sequential-first"
    second_session = "sid-sequential-second"

    async def exercise() -> None:
        ss.reset_project_for_testing()
        ss.clear_session_messages_cache()

        switch_session(first_session)
        with run_context_scope(
            RunContext("p", "a1", first_session, "r1", cwd)
        ):
            await ss.record_transcript(
                [create_user_message(content="first-session-marker")]
            )
            await ss.flush_session_storage()

        switch_session(second_session)
        with run_context_scope(
            RunContext("p", "a2", second_session, "r2", cwd)
        ):
            await ss.record_transcript(
                [create_user_message(content="second-session-marker")]
            )
            await ss.flush_session_storage()

        first_file = os.path.join(project_dir, f"{first_session}.jsonl")
        second_file = os.path.join(project_dir, f"{second_session}.jsonl")
        assert os.path.exists(first_file)
        assert os.path.exists(second_file)
        assert "first-session-marker" in open(first_file, encoding="utf-8").read()
        assert "second-session-marker" not in open(first_file, encoding="utf-8").read()
        assert "second-session-marker" in open(second_file, encoding="utf-8").read()

        with run_context_scope(
            RunContext("p", "a1", first_session, "r3", cwd)
        ):
            resumed = await ss.load_conversation_for_resume(first_session)
        assert resumed[0]["message"]["content"] == "first-session-marker"

    try:
        asyncio.run(exercise())
    finally:
        ss.reset_project_for_testing()
        ss.clear_session_messages_cache()
        switch_session(original_session, project_dir=original_project_dir)


def test_concurrent_runs_route_transcripts_by_run_context(monkeypatch) -> None:
    """Concurrent Gateway runs may stomp bootstrap state but not task-local transcript routing."""
    monkeypatch.setenv("TEST_ENABLE_SESSION_PERSISTENCE", "1")
    original_session = get_session_id()
    original_project_dir = get_session_project_dir()
    cwd = ss.get_original_cwd()
    project_dir = ss.get_project_dir(cwd)
    first_session = "sid-concurrent-first"
    second_session = "sid-concurrent-second"

    async def worker(session_id: str, marker: str, ready: asyncio.Event) -> None:
        switch_session(session_id)
        with run_context_scope(
            RunContext("p", f"a-{session_id}", session_id, f"r-{session_id}", cwd)
        ):
            ready.set()
            await asyncio.sleep(0)
            await ss.record_transcript([create_user_message(content=marker)])

    async def exercise() -> None:
        ss.reset_project_for_testing()
        ss.clear_session_messages_cache()
        first_ready = asyncio.Event()
        second_ready = asyncio.Event()
        await asyncio.gather(
            worker(first_session, "concurrent-first-marker", first_ready),
            worker(second_session, "concurrent-second-marker", second_ready),
        )
        assert first_ready.is_set() and second_ready.is_set()
        await ss.flush_session_storage()

        first_text = open(
            os.path.join(project_dir, f"{first_session}.jsonl"), encoding="utf-8"
        ).read()
        second_text = open(
            os.path.join(project_dir, f"{second_session}.jsonl"), encoding="utf-8"
        ).read()
        assert "concurrent-first-marker" in first_text
        assert "concurrent-second-marker" not in first_text
        assert "concurrent-second-marker" in second_text
        assert "concurrent-first-marker" not in second_text

    try:
        asyncio.run(exercise())
    finally:
        ss.reset_project_for_testing()
        ss.clear_session_messages_cache()
        switch_session(original_session, project_dir=original_project_dir)
