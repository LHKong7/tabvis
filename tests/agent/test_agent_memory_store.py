"""Phase 2 — the revisioned AgentMemoryStore (design §8, §14).

Exercises every Phase 2 acceptance criterion against the real on-disk store (``config_home`` autouse
fixture roots it in a tmp dir): isolation, consent gating, crash-safe CAS commits, rollback,
logical forget (not regenerated), physical erase, and the MEMORY.md bounds.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem.agent_store import (
    AgentMemoryError,
    AgentMemoryStore,
    MemoryConflict,
    MemoryForbidden,
)
from tabvis.agent.mem.schemas import (
    MEMORY_MD_MAX_BYTES,
    MEMORY_MD_MAX_LINES,
    BrowsingTopic,
    Consent,
    MemorySnapshot,
    SessionDigest,
    UserFact,
)


@pytest.fixture(autouse=True)
def _clean_locks() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


def _store(agent: str = "ag_1", principal: str = "principal_local") -> AgentMemoryStore:
    return AgentMemoryStore(principal, agent)


def _snap(fact: str = "Prefer concise answers.", topic: str | None = None,
          session: str | None = None) -> MemorySnapshot:
    return MemorySnapshot(
        user_facts=[UserFact.create(fact)],
        topics=[BrowsingTopic.create(topic, topic, f"summary of {topic}")] if topic else [],
        sessions=[SessionDigest(session_id=session, goal="g")] if session else [],
    )


# --------------------------------------------------------------------------- isolation / authz


def test_agent_a_cannot_address_agent_b() -> None:
    a, b = _store("ag_a"), _store("ag_b")
    a.commit(_snap("A's secret preference."))
    assert [f.text for f in a.get_effective_snapshot().user_facts] == ["A's secret preference."]
    assert b.get_effective_snapshot().user_facts == []          # B sees nothing of A
    assert a.root != b.root


def test_open_for_rejects_cross_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    from tabvis.agent.agents import registry

    registry._records.clear()
    registry._records["ag_owned"] = registry.AgentRecord(
        agent_id="ag_owned", session_id="s", principal_id="principal_owner",
    )
    with pytest.raises(MemoryForbidden):
        AgentMemoryStore.open_for("principal_intruder", "ag_owned")
    # the true owner is allowed
    assert AgentMemoryStore.open_for("principal_owner", "ag_owned").agent_id == "ag_owned"
    registry._records.clear()


def test_principal_scope_is_opaque() -> None:
    assert A.principal_scope("principal_local") == "local"
    assert A.principal_scope("user@example.com").startswith("p_")
    assert "example.com" not in A.principal_scope("user@example.com")  # not a leaked identifier


def test_invalid_agent_id_rejected() -> None:
    with pytest.raises(AgentMemoryError):
        AgentMemoryStore("principal_local", "../escape")


def test_session_id_grants_nothing() -> None:
    # The store never keys on a session id; only (principal, agent) address a namespace.
    s = _store("ag_s")
    s.commit(_snap(session="sess-xyz"))
    # A different agent, even "knowing" sess-xyz, addresses a different (empty) store.
    other = _store("ag_other")
    assert other.get_effective_snapshot().sessions == []


# --------------------------------------------------------------------------- consent


def test_consent_gates_evidence_epoch() -> None:
    s = _store()
    assert s.get_consent().enabled is False
    c = s.grant_consent(evidence_not_before="2026-07-22T00:00:00Z")
    assert c.enabled and c.version == 1
    assert s.get_consent().allows_evidence("2026-07-22T10:00:00Z") is True   # after epoch
    assert s.get_consent().allows_evidence("2026-07-01T00:00:00Z") is False  # before epoch


def test_backfill_authorization_widens_range() -> None:
    s = _store()
    s.grant_consent(evidence_not_before="2026-07-22T00:00:00Z")
    assert s.get_consent().allows_evidence("2026-01-01T00:00:00Z") is False
    s.authorize_backfill(not_before="2026-01-01T00:00:00Z")
    assert s.get_consent().allows_evidence("2026-01-01T00:00:00Z") is True
    assert s.get_consent().allows_evidence("2025-12-31T00:00:00Z") is False


def test_revoke_blocks_and_survives_rollback_semantics() -> None:
    s = _store()
    s.grant_consent()
    s.revoke_consent()
    c = s.get_consent()
    assert c.revoked and not c.enabled
    assert c.allows_evidence("2099-01-01T00:00:00Z") is False
    with pytest.raises(AgentMemoryError):
        s.authorize_backfill(not_before="2026-01-01T00:00:00Z")


def test_consent_lives_outside_revisions() -> None:
    # A rollback cannot roll consent back (consent.json is not part of any revision).
    s = _store()
    r1 = s.commit(_snap("f1"))
    s.grant_consent()
    s.commit(_snap("f2"))
    s.rollback(r1)
    assert s.get_consent().enabled is True  # untouched by rollback


# --------------------------------------------------------------------------- revisions / CAS / crash


def test_commit_and_load_roundtrip() -> None:
    s = _store()
    r1 = s.commit(_snap("hello"))
    assert s.get_current_revision() == r1
    assert [f.text for f in s.load_snapshot(r1).user_facts] == ["hello"]


def test_cas_conflict_when_base_stale() -> None:
    s = _store()
    r1 = s.commit(_snap("a"))
    r2 = s.commit(_snap("b"), base_revision=r1)
    assert s.get_current_revision() == r2
    with pytest.raises(MemoryConflict):
        s.commit(_snap("c"), base_revision=r1)  # r1 is no longer CURRENT
    s.commit(_snap("c"), base_revision=r2)       # correct base succeeds


def test_crash_before_current_switch_keeps_old_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store()
    r1 = s.commit(_snap("stable"))
    monkeypatch.setattr(s, "_set_current", lambda rev: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        s.commit(_snap("doomed"))
    # CURRENT never moved; the old revision is intact and complete.
    assert s.get_current_revision() == r1
    assert [f.text for f in s.get_effective_snapshot().user_facts] == ["stable"]


def test_crash_after_current_switch_before_projection(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _store()
    s.commit(_snap("old"))
    monkeypatch.setattr(s, "_refresh_projections",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):
        s.commit(_snap("new"))
    # CURRENT advanced and the new revision is fully readable (reads use content.json, not the
    # stale top-level projection) — a complete NEW revision, never a half-updated mix.
    assert [f.text for f in s.get_effective_snapshot().user_facts] == ["new"]


# --------------------------------------------------------------------------- forget / rollback / erase


def test_forget_hides_and_is_not_regenerated() -> None:
    s = _store()
    snap = _snap("Remember X.")
    fid = snap.user_facts[0].id
    s.commit(snap)
    s.forget("fact", fid, reason="user asked")
    assert s.get_effective_snapshot().user_facts == []
    # Consolidation re-derives the same fact from retained evidence and commits it again...
    s.commit(_snap("Remember X."))
    # ...it is STILL suppressed (tombstone sits above every revision).
    assert s.get_effective_snapshot().user_facts == []


def test_rollback_never_reexposes_suppressed() -> None:
    s = _store()
    snap = _snap("secret pref")
    fid = snap.user_facts[0].id
    r1 = s.commit(snap)
    s.forget("fact", fid)
    s.commit(_snap("other"))
    s.rollback(r1)  # r1 CONTAINS the forgotten fact
    assert all(f.id != fid for f in s.get_effective_snapshot().user_facts)


def test_logical_forget_keeps_history_physical_erase_removes_it() -> None:
    s = _store()
    snap = MemorySnapshot(topics=[BrowsingTopic.create("t1", "Topic One", "sensitive summary")])
    tid = snap.topics[0].id
    rev = s.commit(snap)

    content_path = os.path.join(s._revision_dir(rev), "content.json")
    topics_dir = os.path.join(s._revision_dir(rev), "topics")
    # logical forget: hidden from reads, but the revision still physically contains it.
    s.forget("topic", tid)
    assert s.get_effective_snapshot().topics == []
    assert "sensitive summary" in open(content_path, encoding="utf-8").read()
    # physical erase: gone from the revision content and its projections too.
    s.erase("topic", tid)
    assert "sensitive summary" not in open(content_path, encoding="utf-8").read()
    assert not os.path.exists(topics_dir) or not os.listdir(topics_dir)


# --------------------------------------------------------------------------- MEMORY.md bounds


def test_memory_index_respects_bounds() -> None:
    s = _store()
    topics = [BrowsingTopic.create(f"topic-{i}", f"Topic {i}", "x" * 200) for i in range(500)]
    s.commit(MemorySnapshot(topics=topics))

    text = open(s._p("MEMORY.md"), encoding="utf-8").read()
    assert len(text.splitlines()) <= MEMORY_MD_MAX_LINES
    assert len(text.encode("utf-8")) <= MEMORY_MD_MAX_BYTES


# --------------------------------------------------------------------------- serialization


def test_snapshot_json_roundtrip() -> None:
    snap = _snap("f", topic="research", session="s1")
    back = MemorySnapshot.from_json(snap.to_json())
    assert [f.text for f in back.user_facts] == ["f"]
    assert [t.title for t in back.topics] == ["research"]
    assert [x.session_id for x in back.sessions] == ["s1"]


def test_consent_json_roundtrip() -> None:
    c = Consent(version=2, enabled=True, evidence_not_before="2026-07-22T00:00:00Z")
    assert Consent.from_dict(c.to_dict()).version == 2
