"""Phase 7 — Session Registry, crash recovery, snapshots & process split (ROADMAP.md).

RT-5/6 (leases, heartbeats, reclaim, stale-lock sweep), PERS-5 (profile snapshot cycle), PERS-6
(checkpoints/replay), PERS-7 (two-phase commit), RT-7 (BrowserAdapter), RT-8/9 (BrowserClient +
worker flag). Everything is deterministic (explicit ``now_ts``) or pure filesystem — no browser and
no wall-clock sleeps. ``config_home`` (autouse) roots on-disk state in a tmp dir.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tabvis.browser import (
    session_registry as sr,
)


@pytest.fixture(autouse=True)
def _clean() -> Any:
    sr._leases.clear()
    yield
    sr._leases.clear()


# --------------------------------------------------------------------------- RT-5 / RT-6


def test_session_lease_heartbeat_and_expiry() -> None:
    lease = sr.acquire("sess-1", "ag_1", now_ts=100.0)
    assert lease.lease_id.startswith("lease_") and lease.status == "active"
    assert lease.is_expired(now_ts=100.0 + sr.DEFAULT_LEASE_TTL_S - 1) is False
    assert lease.is_expired(now_ts=100.0 + sr.DEFAULT_LEASE_TTL_S + 1) is True
    # heartbeat pushes the expiry window forward.
    assert sr.heartbeat("sess-1", now_ts=200.0) is True
    assert sr.get("sess-1").is_expired(now_ts=200.0 + sr.DEFAULT_LEASE_TTL_S - 1) is False


def test_reclaim_crashed_marks_expired() -> None:
    sr.acquire("sess-live", "ag_a", now_ts=1000.0)
    sr.acquire("sess-dead", "ag_b", now_ts=10.0)  # stale heartbeat
    reclaimed = sr.reclaim_crashed(now_ts=1000.0)
    assert reclaimed == ["sess-dead"]
    assert sr.get("sess-dead").status == "crashed"
    assert sr.get("sess-live").status == "active"


def test_lease_persists_and_reloads_across_restart() -> None:
    sr.acquire("sess-persist", "ag_p", now_ts=50.0)
    sr._leases.clear()                                   # simulate a process restart
    assert sr.get("sess-persist") is None
    assert sr.load_persisted_leases() >= 1
    assert sr.get("sess-persist") is not None
    # the reloaded lease (old heartbeat) is reclaimable.
    assert sr.reclaim_crashed(now_ts=50.0 + sr.DEFAULT_LEASE_TTL_S + 10) == ["sess-persist"]


def test_sweep_stale_lock(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("lock")
    (profile / "Default").mkdir()  # real profile data — must NOT be removed
    assert sr.sweep_stale_lock(str(profile)) is True
    assert not (profile / "SingletonLock").exists()
    assert (profile / "Default").is_dir()                # data preserved
    assert sr.sweep_stale_lock(str(profile)) is False    # nothing left to sweep


# --------------------------------------------------------------------------- PERS-5: profile snapshot
