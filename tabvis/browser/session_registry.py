"""Session Registry — leases, heartbeats & crash reclamation (RT-5 / RT-6).

``design.md`` §"Session Registry" + §"故障恢复": the runtime tracks each live browser session with a
lease and a heartbeat, and on restart reclaims sessions whose lease expired (a crashed worker) and
sweeps stale Chromium profile locks. This module is that bookkeeping.

Leases are held in memory and mirrored best-effort to a JSON sidecar under
``browser-os-data/sessions/<session_id>/lease.json`` so a *new* process can see that a prior session
died. Time is threaded through explicitly (``now_ts``) so expiry logic is deterministic and testable
without wall-clock sleeps. Nothing here changes the single-process default — it is additive tracking
that the process split (RT-9) will consume.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from tabvis.browser.persistence.paths import sessions_dir
from tabvis.utils.debug import log_for_debugging

# A session whose heartbeat is older than this (seconds) is considered crashed.
DEFAULT_LEASE_TTL_S = 90.0


def new_lease_id() -> str:
    import uuid

    return "lease_" + uuid.uuid4().hex[:16]


@dataclass
class SessionLease:
    """One live browser session's lease (RT-5)."""

    session_id: str
    agent_id: str
    worker_pid: int
    lease_id: str = field(default_factory=new_lease_id)
    heartbeat_at: float = 0.0
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S
    status: str = "active"  # active | crashed | released

    def is_expired(self, now_ts: float) -> bool:
        return self.status == "active" and (now_ts - self.heartbeat_at) > self.lease_ttl_s

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_leases: dict[str, SessionLease] = {}


def _lease_path(session_id: str) -> str:
    return os.path.join(sessions_dir(session_id), "lease.json")


def _persist(lease: SessionLease) -> None:
    try:
        os.makedirs(sessions_dir(lease.session_id), exist_ok=True)
        path = _lease_path(lease.session_id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(lease.to_dict(), fh)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - best-effort
        log_for_debugging(f"[SESSION] failed to persist lease {lease.session_id}: {e}")


def acquire(session_id: str, agent_id: str, *, now_ts: float, worker_pid: int | None = None) -> SessionLease:
    """Open (or refresh) a lease for a session, stamping the first heartbeat (RT-5)."""
    lease = _leases.get(session_id)
    if lease is None:
        lease = SessionLease(session_id=session_id, agent_id=agent_id, worker_pid=worker_pid or os.getpid())
        _leases[session_id] = lease
    lease.status = "active"
    lease.heartbeat_at = now_ts
    _persist(lease)
    return lease


def heartbeat(session_id: str, *, now_ts: float) -> bool:
    """Extend a session's lease. False if there is no such (active) lease."""
    lease = _leases.get(session_id)
    if lease is None or lease.status != "active":
        return False
    lease.heartbeat_at = now_ts
    _persist(lease)
    return True


def release(session_id: str) -> None:
    """Release a lease cleanly (normal close)."""
    lease = _leases.get(session_id)
    if lease is not None:
        lease.status = "released"
        _persist(lease)


def get(session_id: str) -> SessionLease | None:
    return _leases.get(session_id)


def list_leases() -> list[SessionLease]:
    return list(_leases.values())


def load_persisted_leases() -> int:
    """Load lease sidecars from disk (RT-6, run on startup). Returns how many were loaded."""
    root = sessions_dir()
    if not os.path.isdir(root):
        return 0
    loaded = 0
    for name in os.listdir(root):
        path = os.path.join(root, name, "lease.json")
        if name in _leases or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            _leases[data["session_id"]] = SessionLease(
                session_id=data["session_id"],
                agent_id=data.get("agent_id", ""),
                worker_pid=int(data.get("worker_pid", 0)),
                lease_id=data.get("lease_id", new_lease_id()),
                heartbeat_at=float(data.get("heartbeat_at", 0.0)),
                lease_ttl_s=float(data.get("lease_ttl_s", DEFAULT_LEASE_TTL_S)),
                status=data.get("status", "active"),
            )
            loaded += 1
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[SESSION] skipped unreadable lease {name}: {e}")
    return loaded


def reclaim_crashed(*, now_ts: float) -> list[str]:
    """Mark every active-but-expired lease as ``crashed`` (RT-6). Returns the reclaimed session ids.

    Distinct from a clean ``released`` — a crashed session's profile snapshot is NOT clobbered, and a
    stale Chromium lock in its profile can be swept (:func:`sweep_stale_lock`).
    """
    reclaimed: list[str] = []
    for lease in _leases.values():
        if lease.is_expired(now_ts):
            lease.status = "crashed"
            _persist(lease)
            reclaimed.append(lease.session_id)
    return reclaimed


def sweep_stale_lock(profile_dir: str) -> bool:
    """Remove Chromium's ``SingletonLock`` from a profile dir left by a crashed worker (RT-6).

    Only the symlink/lock file is removed — never profile data. Returns True if a lock was removed.
    Callers must ensure no live process owns the profile (a reclaimed/crashed session).
    """
    removed = False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(profile_dir, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.unlink(path)
                removed = True
        except OSError as e:
            log_for_debugging(f"[SESSION] could not sweep {path}: {e}")
    return removed
