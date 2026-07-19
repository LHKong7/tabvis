"""Phase 2 — durable metadata & identity records (ROADMAP.md).

Covers PERS-2 (SQLite ``runtime.db`` shadow), PERS-3 (SQLite read authority for the agent cold-load +
JSON→SQLite backfill, JSON still source of truth), PERS-4 (artifact index rows), IDP-2 (durable
agent-keyed identity store), IDP-3 (identity materialized at spawn), and WS-2 (workspace→identity
indirection). ``config_home`` (autouse) roots everything in a tmp dir, so the DB / sidecars never
touch the real one.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from tabvis.agent.agents import registry as r
from tabvis.browser import identity_store, workspace as ws
from tabvis.browser import manager as mgr
from tabvis.browser.persistence import db


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset every process-global the durable stores touch, and reopen the DB per (tmp) config home."""
    for reset in (_reset,):
        reset()
    yield
    _reset()


def _reset() -> None:
    r._records.clear()
    r._tasks.clear()
    r._persisted_loaded = False
    identity_store._cache.clear()
    ws._by_id.clear()
    ws._id_by_agent.clear()
    mgr._workspaces.clear()
    mgr._slots.clear()
    db.close()


# --------------------------------------------------------------------------- PERS-2: SQLite store


def test_db_agent_round_trip() -> None:
    assert db.is_sqlite_enabled() is True
    db.upsert_agent({"agent_id": "ag_db1", "session_id": "s1", "status": "completed", "model": "m"})
    got = db.get_agent("ag_db1")
    assert got is not None and got["session_id"] == "s1" and got["status"] == "completed"
    assert any(a["agent_id"] == "ag_db1" for a in db.list_agents())
    # upsert overwrites the same primary key (no duplicate row).
    db.upsert_agent({"agent_id": "ag_db1", "session_id": "s1", "status": "failed"})
    assert db.get_agent("ag_db1")["status"] == "failed"
    assert sum(1 for a in db.list_agents() if a["agent_id"] == "ag_db1") == 1


def test_db_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_BROWSER_SQLITE", "0")
    assert db.is_sqlite_enabled() is False
    db.upsert_agent({"agent_id": "ag_off", "status": "completed"})
    assert db.get_agent("ag_off") is None      # no-op read/write
    assert db.list_agents() == []


def test_db_session_and_artifact_rows() -> None:
    db.upsert_session("sess-1", {"agent": {"session_id": "sess-1"}, "status": "ready", "browser": {"engine": "chromium"}})
    assert db.get_session("sess-1")["status"] == "ready"
    db.insert_artifact("sess-1", "ag_1", {"seq": 1, "type": "navigation", "url": "https://a.com"})
    db.insert_artifact("sess-1", "ag_1", {"seq": 2, "type": "page", "url": "https://a.com/x"})
    rows = db.list_artifacts("sess-1")
    assert [row["seq"] for row in rows] == [1, 2]
    assert rows[0]["type"] == "navigation"


# --------------------------------------------------------------------------- PERS-2/3: registry


def test_registry_persist_shadows_into_sqlite() -> None:
    rec = r.create(agent_id="ag_sh", session_id="s", prompt="p")
    rec.status = "completed"
    asyncio.run(r.persist(rec))
    assert db.get_agent("ag_sh") is not None      # mirrored alongside the JSON sidecar
    assert os.path.exists(r.record_path("ag_sh"))  # JSON still written (source of truth)


def test_registry_reads_sqlite_only_record_on_restart() -> None:
    # A record that exists ONLY in SQLite (no JSON sidecar) — proves SQLite is a read authority.
    db.upsert_agent(
        {"agent_id": "ag_sqlite_only", "session_id": "s9", "status": "completed", "profile": "work", "prompt": "hi"}
    )
    assert not os.path.exists(r.record_path("ag_sqlite_only"))
    got = r.get("ag_sqlite_only")
    assert got is not None
    assert got.session_id == "s9" and got.profile == "work" and got.status == "completed"


def test_registry_backfills_json_only_record_into_sqlite() -> None:
    # A legacy record present only as JSON gets mirrored into SQLite on load (idempotent backfill).
    os.makedirs(r.agents_dir(), exist_ok=True)
    import json

    with open(r.record_path("ag_legacy"), "w", encoding="utf-8") as fh:
        json.dump({"agent_id": "ag_legacy", "session_id": "s", "status": "completed", "prompt": "p"}, fh)
    assert db.get_agent("ag_legacy") is None
    r.get("ag_legacy")                               # triggers lazy load + backfill
    assert db.get_agent("ag_legacy") is not None     # now in the DB too


def test_registry_sqlite_running_normalized_to_failed() -> None:
    # A stale-running record in SQLite must normalize to failed on load, like the JSON path.
    db.upsert_agent({"agent_id": "ag_stale", "session_id": "s", "status": "running", "prompt": "p"})
    got = r.get("ag_stale")
    assert got is not None and got.status == "failed" and got.error


# --------------------------------------------------------------------------- IDP-2: identity store


def test_identity_resolve_creates_and_persists() -> None:
    ident = identity_store.resolve("ag_id1", profile_ref="/tmp/prof")
    assert ident.id.startswith("id_")
    assert ident.agent_id == "ag_id1"
    assert ident.profile.profile_ref == "/tmp/prof"
    # sidecar written (source of truth) + mirrored into SQLite.
    assert os.path.exists(identity_store._path("ag_id1"))
    assert db.get_identity_by_agent("ag_id1") is not None
    # resolve again → SAME identity (agent↔identity 1:1), even from a cold cache.
    identity_store._cache.clear()
    again = identity_store.resolve("ag_id1")
    assert again.id == ident.id
    assert again.profile.profile_ref == "/tmp/prof"   # loaded back from disk


def test_identity_get_by_agent_does_not_create() -> None:
    assert identity_store.get_by_agent("ag_never") is None
    assert not os.path.exists(identity_store._path("ag_never"))


def test_identity_update_for_agent_patches_fields() -> None:
    identity_store.resolve("ag_upd", profile_ref="/tmp/p")
    updated = identity_store.update_for_agent(
        "ag_upd", {"name": "research", "status": "in_use", "environment": {"locale": "en-US"}}
    )
    assert updated.name == "research"
    assert updated.status == "in_use"
    assert updated.environment.locale == "en-US"
    # immutable owners are ignored even if present in the patch.
    keep_id = updated.id
    updated2 = identity_store.update_for_agent("ag_upd", {"id": "id_hacked", "agent_id": "ag_other"})
    assert updated2.id == keep_id and updated2.agent_id == "ag_upd"


# --------------------------------------------------------------------------- IDP-3 / WS-2: spawn wiring


def test_spawn_materializes_identity_and_links_workspace() -> None:
    mgr.init_browser_session(session_id="s1", model="m", cwd="/tmp", agent_id="ag_spawn", profile="myprof")

    ident = identity_store.get_by_agent("ag_spawn")
    assert ident is not None                                  # IDP-3: materialized at spawn
    assert ident.profile.profile_ref and ident.profile.profile_ref.endswith("myprof")

    wsr = ws.get_workspace_for_agent("ag_spawn")
    assert wsr is not None
    assert wsr.identity_id == ident.id                        # WS-2: workspace → identity indirection
    assert wsr.identity_ref.endswith("myprof")                # dir ref still present (no-op split)

    snap = ws.snapshot(wsr.workspace_id)
    assert snap["identity_id"] == ident.id
