"""Phase 3 — EvidenceCheckpoint + MemoryConsolidator (design §7.2, §9, §10).

Every Phase 3 acceptance criterion is exercised against the real Phase 2 store with a fake extractor
(no model): idempotent re-run, CAS/rebase preserving a concurrent commit, invalid-extraction not
advancing the checkpoint, consolidation failure keeping the Run OK, no raw DOM in Memory, deterministic
session digests, and candidate validation.
"""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem import consolidator as C
from tabvis.agent.mem.agent_store import AgentMemoryStore, MemoryConflict
from tabvis.agent.mem.evidence import build_checkpoint, collect_evidence
from tabvis.agent.mem.schemas import MemorySnapshot, UserFact


@pytest.fixture(autouse=True)
def _clean() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


def _store(agent: str = "ag_1") -> AgentMemoryStore:
    s = AgentMemoryStore("principal_local", agent)
    s.grant_consent()
    return s


_MSGS = [
    {"type": "user", "uuid": "u1", "message": {"content": "Always prefer primary docs."}},
    {"type": "assistant", "uuid": "a1", "message": {"content": [{"type": "text", "text": "Recommend X."}]}},
]
_ARTS = [
    {"seq": 1, "type": "navigation", "url": "https://user:pw@example.com/docs?token=abc#f", "title": "Docs",
     "dom_ref": "dom/deadbeef.html"},
    {"seq": 2, "type": "download", "url": "https://x.test/r.pdf", "filename": "r.pdf",
     "sha256": "d" * 64, "size_bytes": 10, "policy_effect": "allow"},
]


def _checkpoint(store: AgentMemoryStore, *, status: str = "completed", run_id: str = "run_1") -> Any:
    return build_checkpoint(run_id=run_id, agent_id=store.agent_id, session_id="sess1",
                            status=status, messages=_MSGS, artifacts=_ARTS)


def _evidence() -> Any:
    return collect_evidence("sess1", _MSGS, _ARTS)


def _extractor(facts: list[dict] | None = None, topics: list[dict] | None = None,
               digest: dict | None = None) -> C.Extractor:
    async def extract(_packet: dict[str, Any]) -> dict[str, Any]:
        return {
            "sessionDigest": digest or {"goal": "Compare libraries", "confirmedConclusions": ["X wins"]},
            "userFacts": facts if facts is not None else
                [{"statement": "Prefer primary docs.", "sourceMessageUuid": "u1", "explicit": True}],
            "browsingTopics": topics if topics is not None else
                [{"topicKey": "libs", "summary": "Comparing X and Y.", "confidence": 0.8}],
        }
    return extract


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


# --------------------------------------------------------------------------- checkpoint / evidence


def test_checkpoint_freezes_high_water_and_refs() -> None:
    ck = _checkpoint(_store())
    assert ck.transcript_head_uuid == "a1"
    assert ck.artifact_high_water_seq == 2
    assert ck.transcript_digest and ck.artifact_tail_digest
    assert len(ck.download_refs) == 1 and ck.download_refs[0]["sha256"] == "d" * 64


def test_evidence_is_sanitized_and_has_no_raw_dom() -> None:
    ev = _evidence()
    d = ev.to_extractor_dict()
    assert d["navigations"] == [{"origin": "https://example.com", "path": "/docs", "title": "Docs"}]
    assert "dom" not in json_str(d) and "token" not in json_str(d) and "user:pw" not in json_str(d)
    assert ev.user_message_uuids() == {"u1"}


# --------------------------------------------------------------------------- happy path + idempotency


def test_consolidate_commits_facts_topics_and_digest() -> None:
    store = _store()
    res = _run(C.consolidate_run(store, _checkpoint(store), _evidence(), extractor=_extractor()))
    assert res.status == "committed" and res.revision
    eff = store.get_effective_snapshot()
    assert [f.text for f in eff.user_facts] == ["Prefer primary docs."]
    assert [t.title for t in eff.topics] == ["libs"]
    assert [s.goal for s in eff.sessions] == ["Compare libraries"]


def test_same_job_twice_is_a_noop() -> None:
    store = _store()
    ck, ev = _checkpoint(store), _evidence()
    r1 = _run(C.consolidate_run(store, ck, ev, extractor=_extractor()))
    r2 = _run(C.consolidate_run(store, ck, ev, extractor=_extractor()))
    assert r1.status == "committed" and r2.status == "duplicate"
    # exactly one fact — the second run did not re-append.
    assert len(store.get_effective_snapshot().user_facts) == 1


def test_no_raw_dom_in_committed_memory() -> None:
    store = _store()
    _run(C.consolidate_run(store, _checkpoint(store), _evidence(), extractor=_extractor()))
    rev = store.get_current_revision()
    content = open(store._p("revisions", rev, "content.json"), encoding="utf-8").read()
    assert "dom/deadbeef" not in content and "<html" not in content


# --------------------------------------------------------------------------- CAS / rebase


def test_cas_rebase_preserves_concurrent_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store()
    # seed an initial fact A
    store.commit(MemorySnapshot(user_facts=[UserFact.create("Fact A.")]))

    real_commit = AgentMemoryStore.commit
    calls = {"n": 0}

    def flaky(self: AgentMemoryStore, snap: MemorySnapshot, *, base_revision: object = A._UNSET) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            # a concurrent writer lands fact B on top of the base this attempt is using
            other = AgentMemoryStore("principal_local", store.agent_id)
            base = other.get_current_revision()
            osnap = other.load_snapshot(base)
            osnap.user_facts.append(UserFact.create("Fact B."))
            real_commit(other, osnap, base_revision=base)
            raise MemoryConflict("race")
        return real_commit(self, snap, base_revision=base_revision)

    monkeypatch.setattr(AgentMemoryStore, "commit", flaky)
    res = _run(C.consolidate_run(store, _checkpoint(store), _evidence(),
                                 extractor=_extractor(facts=[
                                     {"statement": "Fact C.", "sourceMessageUuid": "u1", "explicit": True}])))
    assert res.status == "committed"
    texts = {f.text for f in store.get_effective_snapshot().user_facts}
    assert texts == {"Fact A.", "Fact B.", "Fact C."}  # the raced-in B was NOT overwritten


# --------------------------------------------------------------------------- invalid / failure


def test_invalid_extraction_does_not_advance_checkpoint() -> None:
    store = _store()
    ck, ev = _checkpoint(store), _evidence()
    # a userFact with no real provenance is rejected by validation
    bad = _run(C.consolidate_run(store, ck, ev, extractor=_extractor(
        facts=[{"statement": "made up", "sourceMessageUuid": "nope", "explicit": True}])))
    assert bad.status == "failed"
    assert store.get_current_revision() is None  # nothing committed
    # a later valid run reprocesses (the failed job did not advance the checkpoint)
    good = _run(C.consolidate_run(store, ck, ev, extractor=_extractor()))
    assert good.status == "committed"


def test_consolidation_failure_never_raises() -> None:
    store = _store()

    async def boom(_p: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("model exploded")

    res = _run(C.consolidate_run(store, _checkpoint(store), _evidence(), extractor=boom))
    assert res.status == "failed"  # returned, not raised — the Run stays successful


def test_skipped_when_consent_absent() -> None:
    store = AgentMemoryStore("principal_local", "ag_noconsent")  # no grant_consent
    res = _run(C.consolidate_run(store, _checkpoint(store), _evidence(), extractor=_extractor()))
    assert res.status == "skipped"


# --------------------------------------------------------------------------- session digest / statuses


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_session_digest_updates_for_terminal_status_without_model(status: str) -> None:
    store = _store()
    # no extractor + a non-completed status still records the Session Digest deterministically
    res = _run(C.consolidate_run(store, _checkpoint(store, status=status), _evidence(), extractor=None))
    assert res.status == "committed"
    sessions = store.get_effective_snapshot().sessions
    assert len(sessions) == 1 and sessions[0].status == status


# --------------------------------------------------------------------------- validation unit


def test_validate_rejects_unknown_fields() -> None:
    with pytest.raises(C.ConsolidationError):
        C.validate_candidates({"userFacts": [], "surprise": 1}, _evidence())


def test_validate_rejects_secret_like_fact() -> None:
    with pytest.raises(C.ConsolidationError):
        C.validate_candidates(
            {"userFacts": [{"statement": "sk_live_0123456789abcdefgh", "sourceMessageUuid": "u1",
                            "explicit": True}]}, _evidence())


def test_validate_rejects_bad_confidence() -> None:
    with pytest.raises(C.ConsolidationError):
        C.validate_candidates(
            {"browsingTopics": [{"topicKey": "k", "summary": "s", "confidence": 5}]}, _evidence())


def test_validate_rejects_non_user_provenance() -> None:
    with pytest.raises(C.ConsolidationError):
        C.validate_candidates(
            {"userFacts": [{"statement": "x", "sourceMessageUuid": "a1", "explicit": True}]}, _evidence())


# --------------------------------------------------------------------------- job recovery


def test_recover_pending_jobs() -> None:
    store = _store()

    async def boom(_p: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("fail once")

    _run(C.consolidate_run(store, _checkpoint(store), _evidence(), extractor=boom))  # -> failed job
    pending = C.recover_pending_jobs(store)
    assert len(pending) == 1 and pending[0].status == "failed"


def json_str(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str)
