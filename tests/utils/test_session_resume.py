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

from tabvis.utils import session_storage as ss


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
