"""Optional process separation — worker registration, leases, placement (design §15 Phase 8, §2.2).

Process separation is **not required** for the initial gateway (design §15/§18.1), so this is the
coordination scaffolding a split would use, built in-process and testable, preserving the same
Command/Event protocol: workers register and heartbeat (a lease, exactly like a browser lease), the
coordinator *places* a Run on a ready worker with spare capacity, and a worker whose heartbeat lapses is
reclaimed — its placed Runs surfaced so the orchestrator can mark them ``interrupted`` (design §7.4
running → interrupted "worker lost", §1.5 lease-backed recovery).

Durable in ``gateway.db`` so the gateway tracks workers across its own restart. The clock is injected so
lease expiry is deterministic in tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.store import db
from tabvis.utils.debug import log_for_debugging

# worker kinds (design §2.2)
KIND_AGENT = "agent"
KIND_BROWSER = "browser"

# worker status
READY = "ready"
DRAINING = "draining"
LOST = "lost"
STOPPED = "stopped"

PLACED = "placed"
RELEASED = "released"

DEFAULT_TTL_SECONDS = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class WorkerRecord:
    worker_id: str
    kind: str
    status: str
    max_slots: int
    labels: dict[str, str] = field(default_factory=dict)
    registered_at: str = ""
    heartbeat_at: str = ""
    expires_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkerRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class WorkerRegistry:
    def __init__(self, clock: Callable[[], datetime] | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._clock = clock or _utc_now
        self._ttl = ttl_seconds

    def _expiry(self, now: datetime) -> str:
        return (now + timedelta(seconds=self._ttl)).isoformat()

    @staticmethod
    def _expired(worker: WorkerRecord, now: datetime) -> bool:
        try:
            return now > datetime.fromisoformat(worker.expires_at)
        except (ValueError, TypeError):
            return True

    def register(self, worker_id: str, *, kind: str, max_slots: int = 1, labels: dict[str, str] | None = None) -> WorkerRecord:
        now = self._clock()
        with db.transaction() as conn:
            existing = db.get_worker_in(conn, worker_id)
            registered_at = existing.get("registered_at") if existing else now.isoformat()
            worker = WorkerRecord(
                worker_id=worker_id, kind=kind, status=READY, max_slots=max_slots,
                labels=labels or {}, registered_at=registered_at or now.isoformat(),
                heartbeat_at=now.isoformat(), expires_at=self._expiry(now),
            )
            db.upsert_worker(conn, worker.to_dict())
        return worker

    def heartbeat(self, worker_id: str) -> WorkerRecord:
        now = self._clock()
        with db.transaction() as conn:
            data = db.get_worker_in(conn, worker_id)
            if data is None:
                raise GatewayError("WORKER_NOT_FOUND", details={"worker_id": worker_id})
            worker = WorkerRecord.from_dict(data)
            # A heartbeat revives a worker that was marked lost after a transient lapse.
            if worker.status in (LOST, READY):
                worker.status = READY
            worker.heartbeat_at = now.isoformat()
            worker.expires_at = self._expiry(now)
            db.update_worker(conn, worker.to_dict())
            return worker

    def deregister(self, worker_id: str) -> None:
        with db.transaction() as conn:
            data = db.get_worker_in(conn, worker_id)
            if data is None:
                return
            worker = WorkerRecord.from_dict(data)
            worker.status = STOPPED
            db.update_worker(conn, worker.to_dict())

    def reclaim_expired(self) -> list[str]:
        """Mark ready workers whose heartbeat lapsed as lost; returns their ids (design §1.5)."""
        now = self._clock()
        lost: list[str] = []
        with db.transaction() as conn:
            for data in _all_ready(conn):
                worker = WorkerRecord.from_dict(data)
                if self._expired(worker, now):
                    worker.status = LOST
                    db.update_worker(conn, worker.to_dict())
                    lost.append(worker.worker_id)
        return lost

    def get(self, worker_id: str) -> WorkerRecord | None:
        data = db.get_worker(worker_id)
        return WorkerRecord.from_dict(data) if data else None


def _all_ready(conn) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT data FROM workers WHERE status = 'ready'").fetchall()
    import json

    return [json.loads(r["data"]) for r in rows]


class WorkerCoordinator:
    """Places Runs on workers and reclaims a lost worker's Runs (design §15 Phase 8)."""

    def __init__(self, registry: WorkerRegistry | None = None, run_store: RunStore | None = None) -> None:
        self.registry = registry or WorkerRegistry()
        self._runs = run_store or get_run_store()

    def place(self, run_id: str, *, kind: str, labels_required: dict[str, str] | None = None) -> WorkerRecord:
        """Assign ``run_id`` to the least-loaded ready worker of ``kind`` with spare capacity.

        Idempotent: a run already placed on a live worker returns that worker. Raises
        ``NO_WORKER_AVAILABLE`` when nothing ready has a free slot.
        """
        now = self.registry._clock()
        with db.transaction() as conn:
            existing = db.get_placement_in(conn, run_id)
            if existing is not None and existing.get("status") == PLACED:
                worker = db.get_worker_in(conn, existing["worker_id"])
                if worker and worker.get("status") == READY:
                    return WorkerRecord.from_dict(worker)

            candidates = [
                WorkerRecord.from_dict(w)
                for w in db.list_workers_by_kind(conn, kind, READY)
                if not WorkerRegistry._expired(WorkerRecord.from_dict(w), now)
                and _labels_match(w, labels_required)
            ]
            best: WorkerRecord | None = None
            best_used = 0
            for worker in candidates:
                used = db.count_active_placements(conn, worker.worker_id)
                if used < worker.max_slots and (best is None or used < best_used):
                    best, best_used = worker, used
            if best is None:
                raise GatewayError("NO_WORKER_AVAILABLE", details={"kind": kind})

            db.upsert_placement(conn, {
                "run_id": run_id, "worker_id": best.worker_id, "status": PLACED,
                "placed_at": now.isoformat(),
            })
            return best

    def release(self, run_id: str) -> None:
        with db.transaction() as conn:
            data = db.get_placement_in(conn, run_id)
            if data is None or data.get("status") != PLACED:
                return
            data["status"] = RELEASED
            db.upsert_placement(conn, data)

    def reclaim(self) -> list[str]:
        """Reclaim lost workers and release their placements; returns the affected run ids."""
        lost_workers = self.registry.reclaim_expired()
        affected: list[str] = []
        with db.transaction() as conn:
            for worker_id in lost_workers:
                for placement in db.active_placements_for_worker(conn, worker_id):
                    placement["status"] = RELEASED
                    db.upsert_placement(conn, placement)
                    affected.append(placement["run_id"])
        return affected

    def worker_for(self, run_id: str) -> str | None:
        placement = db.get_placement(run_id)
        return placement["worker_id"] if placement and placement.get("status") == PLACED else None


def _labels_match(worker: dict[str, Any], required: dict[str, str] | None) -> bool:
    if not required:
        return True
    labels = worker.get("labels") or {}
    return all(labels.get(k) == v for k, v in required.items())


def recover_lost_runs(coordinator: WorkerCoordinator, run_store: RunStore | None = None) -> list[str]:
    """Reclaim lost workers and mark their still-active Runs ``interrupted`` (design §7.4, §1.5).

    Returns the run ids actually interrupted.
    """
    rs = run_store or get_run_store()
    interrupted: list[str] = []
    for run_id in coordinator.reclaim():
        record = rs.get_run(run_id)
        if record is None or record.is_terminal:
            continue
        # Only a running Run can be interrupted (design §7.4); a placed-but-not-started run is simply
        # freed for re-placement.
        if not runs.can_transition(record.status, runs.INTERRUPTED):
            continue
        try:
            rs.transition(run_id, runs.INTERRUPTED, expected=record.status,
                          event_type=EventType.RUN_INTERRUPTED, error_code="worker_lost")
            interrupted.append(run_id)
        except GatewayError as e:  # a concurrent terminalization won the race — fine
            log_for_debugging(f"[GATEWAY] could not interrupt lost run {run_id}: {e}")
    return interrupted
