"""Phase 5 — memory inspect/edit/forget + reference-aware retention (design §14, §18.3)."""

from __future__ import annotations

from typing import Any

import pytest

from tabvis.agent.mem import agent_store as A
from tabvis.agent.mem import memory_admin as MA
from tabvis.agent.mem import retention as R
from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.consolidator import ConsolidationJob, JobStore
from tabvis.agent.mem.schemas import BrowsingTopic, MemorySnapshot


@pytest.fixture(autouse=True)
def _clean() -> Any:
    A._locks.clear()
    yield
    A._locks.clear()


def _store(agent: str = "ag_1") -> AgentMemoryStore:
    s = AgentMemoryStore("principal_local", agent)
    s.grant_consent()
    return s


# --------------------------------------------------------------------------- inspect / edit / forget


def test_add_inspect_forget_roundtrip() -> None:
    _store()
    MA.add_user_fact("principal_local", "ag_1", "Prefer concise answers.")
    view = MA.inspect("principal_local", "ag_1")
    assert [f["text"] for f in view["user_facts"]] == ["Prefer concise answers."]
    assert view["consent"]["enabled"] is True and view["revision"]

    fid = view["user_facts"][0]["id"]
    MA.forget("principal_local", "ag_1", "fact", fid)
    assert MA.inspect("principal_local", "ag_1")["user_facts"] == []


def test_add_user_fact_is_idempotent_by_text() -> None:
    _store()
    r1 = MA.add_user_fact("principal_local", "ag_1", "Same preference.")
    r2 = MA.add_user_fact("principal_local", "ag_1", "Same preference.")
    assert r1["fact_id"] == r2["fact_id"]  # stable id → re-confirm, not duplicate
    assert len(MA.inspect("principal_local", "ag_1")["user_facts"]) == 1


def test_erase_is_physical() -> None:
    store = _store()
    snap = MemorySnapshot(topics=[BrowsingTopic.create("t", "Secret", "sensitive detail")])
    tid = snap.topics[0].id
    rev = store.commit(snap)
    MA.forget("principal_local", "ag_1", "topic", tid, erase=True)
    import os

    content = open(os.path.join(store._revision_dir(rev), "content.json"), encoding="utf-8").read()
    assert "sensitive detail" not in content


def test_revoke_consent_blocks() -> None:
    _store()
    MA.revoke_consent("principal_local", "ag_1")
    assert MA.inspect("principal_local", "ag_1")["consent"]["revoked"] is True


# --------------------------------------------------------------------------- retention


def test_prune_keeps_current_and_window() -> None:
    store = _store()
    for i in range(15):
        store.commit(MemorySnapshot(topics=[BrowsingTopic.create(f"t{i}", f"T{i}", "s")]))
    current = store.get_current_revision()
    pruned = R.prune_revisions(store, keep_last=5)
    remaining = set(store.iter_revisions())
    assert current in remaining                    # CURRENT never pruned
    assert current not in pruned
    assert len(remaining) <= 6                      # window + current
    # CURRENT still loads after pruning
    assert store.get_effective_snapshot().topics[0].title == "T14"


def test_prune_is_reference_aware() -> None:
    store = _store()
    revs = [store.commit(MemorySnapshot(topics=[BrowsingTopic.create(f"t{i}", f"T{i}", "s")]))
            for i in range(10)]
    old_rev = revs[0]
    # A pending job references the OLDEST revision — it must be protected regardless of the window.
    JobStore(store).save(ConsolidationJob(
        job_id="job_ref", agent_id="ag_1", session_id="s", run_id="r",
        status="pending", committed_revision=old_rev))
    R.prune_revisions(store, keep_last=2)
    assert old_rev in set(store.iter_revisions())   # protected by the pending job


def test_prune_committed_jobs() -> None:
    store = _store()
    js = JobStore(store)
    for i in range(60):
        js.save(ConsolidationJob(job_id=f"job_{i:03d}", agent_id="ag_1", session_id="s",
                                 run_id=f"r{i}", status="committed"))
    js.save(ConsolidationJob(job_id="job_pending", agent_id="ag_1", session_id="s",
                             run_id="rp", status="pending"))
    pruned = R.prune_committed_jobs(store, keep_last=10)
    assert len(pruned) == 50
    assert js.get("job_pending") is not None        # pending never pruned


def test_sweep_reports_result() -> None:
    store = _store()
    for i in range(8):
        store.commit(MemorySnapshot(topics=[BrowsingTopic.create(f"t{i}", f"T{i}", "s")]))
    res = R.sweep(store, keep_revisions=3, keep_jobs=5)
    assert res.kept_revisions <= 4 and isinstance(res.pruned_revisions, list)
