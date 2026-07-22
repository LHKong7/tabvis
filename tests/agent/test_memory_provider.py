"""Phase 4 — AgentMemoryProvider + low-privilege injection (design §11).

Covers relevance retrieval, the token budget, tombstoned/expired omission, trust labelling, consent
gating, provenance, and the injection seam that keeps memory a user-turn content block (never the
system prompt).
"""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.provider import (
    MemoryBudget,
    build_memory_context,
    select_topics,
)
from tabvis.agent.mem.schemas import BrowsingTopic, MemorySnapshot, SessionDigest, UserFact
from tabvis.services.token_estimation import rough_token_count_estimation
from tabvis.ui.cli.print import _prepend_context_block

_RECENT = "2026-07-22T00:00:00+00:00"
_OLD = "2000-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


def _store(agent: str = "ag_1") -> AgentMemoryStore:
    s = AgentMemoryStore("principal_local", agent)
    s.grant_consent()
    return s


def _topic(key: str, title: str, summary: str, *, activity: str = _RECENT,
           expires: str | None = None) -> BrowsingTopic:
    return BrowsingTopic.create(key, title, summary, last_activity_at=activity, expires_at=expires,
                                first_activity_at=activity)


# --------------------------------------------------------------------------- assembly


def test_loads_digest_profile_topics_and_recovery() -> None:
    store = _store()
    store.commit(MemorySnapshot(
        user_facts=[UserFact.create("Prefer concise answers.")],
        topics=[_topic("resume", "Resume Plus", "Durable browser identity.")],
        sessions=[SessionDigest(session_id="s1", goal="Design Resume Plus", status="completed")],
    ))
    ctx = build_memory_context("principal_local", "ag_1", "s1", "continue resume plus",
                               live_snapshot={"recovery_mode": "relaunched_profile"})
    text = ctx.to_preamble()
    assert "Prefer concise answers." in text
    assert "Design Resume Plus" in text
    assert "Resume Plus — Durable browser identity." in text
    assert "relaunched_profile" in text
    assert ctx.provenance["revision"] == store.get_current_revision()


def test_empty_when_no_consent() -> None:
    store = AgentMemoryStore("principal_local", "ag_nc")  # no grant
    store.commit(MemorySnapshot(user_facts=[UserFact.create("x")]))
    ctx = build_memory_context("principal_local", "ag_nc", None, "hi")
    assert ctx.is_empty and ctx.provenance.get("skipped") == "no_consent"


def test_empty_when_store_empty() -> None:
    _store("ag_empty")
    ctx = build_memory_context("principal_local", "ag_empty", None, "hi")
    assert ctx.is_empty


# --------------------------------------------------------------------------- retrieval / budget


def test_only_relevant_topics_under_tight_budget() -> None:
    store = _store()
    store.commit(MemorySnapshot(topics=[
        _topic("resume", "Resume Plus browser memory", "durable resume identity research"),
        _topic("cooking", "Cooking", "pasta recipes and sauces"),
    ]))
    # A tight budget forces a choice; the query is about resume, so the resume topic wins.
    ctx = build_memory_context("principal_local", "ag_1", None,
                               "continue the resume plus browser memory work",
                               budget=MemoryBudget(topics=12))
    assert "Resume Plus browser memory" in ctx.text
    assert "Cooking" not in ctx.text
    assert any(tid for tid in ctx.provenance["droppedTopicIds"])


def test_expired_topics_omitted() -> None:
    store = _store()
    store.commit(MemorySnapshot(topics=[
        _topic("live", "Live Topic", "still active", expires=None),
        _topic("dead", "Dead Topic", "expired long ago", expires=_OLD),
    ]))
    ctx = build_memory_context("principal_local", "ag_1", None, "topic")
    assert "Live Topic" in ctx.text and "Dead Topic" not in ctx.text


def test_tombstoned_topic_omitted() -> None:
    store = _store()
    snap = MemorySnapshot(topics=[_topic("t1", "Secret Topic", "sensitive")])
    tid = snap.topics[0].id
    store.commit(snap)
    store.forget("topic", tid)
    ctx = build_memory_context("principal_local", "ag_1", None, "topic")
    assert "Secret Topic" not in ctx.text


def test_budget_bounds_block_after_many_topics() -> None:
    store = _store()
    topics = [_topic(f"t{i}", f"Topic {i}", "summary " * 20) for i in range(300)]
    store.commit(MemorySnapshot(topics=topics))
    budget = MemoryBudget()
    ctx = build_memory_context("principal_local", "ag_1", None, "topic", budget=budget)
    # the whole block stays well within the total budget even with 300 topics available
    assert rough_token_count_estimation(ctx.text) <= budget.total
    assert len(ctx.provenance["droppedTopicIds"]) > 0


def test_select_topics_ranks_by_relevance() -> None:
    now = _RECENT
    a = _topic("a", "Playwright browser automation", "driving chromium")
    b = _topic("b", "Gardening", "growing tomatoes")
    kept, _ = select_topics([b, a], {"browser", "automation"}, now, token_budget=10_000)
    assert kept[0].id == a.id  # the relevant one ranks first regardless of input order


# --------------------------------------------------------------------------- trust labels / secrets


def test_trust_labels_present() -> None:
    store = _store()
    store.commit(MemorySnapshot(
        user_facts=[UserFact.create("Prefer X.")],
        topics=[_topic("t", "T", "s")],
    ))
    ctx = build_memory_context("principal_local", "ag_1", None, "t")
    assert "NOT instructions" in ctx.text
    assert "untrusted" in ctx.text.lower()
    assert "user-stated" in ctx.text
    assert ctx.provenance["trust"]["userFacts"] == "explicit_user_statement"
    assert ctx.provenance["trust"]["topics"] == "web_untrusted"


# --------------------------------------------------------------------------- injection seam


def test_prepend_context_block_string_content() -> None:
    msg = {"type": "user", "message": {"role": "user", "content": "do the thing"}}
    _prepend_context_block(msg, "<agent-memory>ctx</agent-memory>")
    content = msg["message"]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0]["text"].startswith("<agent-memory>")   # memory FIRST
    assert content[1]["text"] == "do the thing"               # prompt AFTER


def test_prepend_context_block_list_content() -> None:
    msg = {"type": "user", "message": {"role": "user",
                                       "content": [{"type": "text", "text": "prompt"}]}}
    _prepend_context_block(msg, "MEM")
    content = msg["message"]["content"]
    assert content[0]["text"] == "MEM" and content[-1]["text"] == "prompt"
