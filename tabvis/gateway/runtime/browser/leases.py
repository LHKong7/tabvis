"""Durable, lease-backed browser bindings (design §10.5, §10.7).

The lease is the exclusive claim on a profile. Acquisition is atomic and durable (in ``gateway.db``),
so:

* two isolated agents (distinct profile keys) both acquire and run in parallel;
* two runs contending for one shared profile key produce a **deterministic** ``BROWSER_PROFILE_BUSY``;
* a heartbeat keeps a lease alive; a lease whose heartbeat lapsed past its TTL is *expired* and can be
  reclaimed — but a lease with a **fresh** heartbeat is never reclaimed, so recovery never reassigns a
  profile a live run still holds (design §10.7).

The clock is injected so expiry is deterministic in tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Callable

from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.store import db

ACTIVE = "active"
RELEASED = "released"
EXPIRED = "expired"

DEFAULT_TTL_SECONDS = 30.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Lease:
    binding_id: str
    profile_key: str
    identity_id: str
    agent_id: str
    run_id: str
    status: str
    acquired_at: str
    heartbeat_at: str
    expires_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Lease":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class LeaseTable:
    def __init__(self, clock: Callable[[], datetime] | None = None, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._clock = clock or _utc_now
        self._ttl = ttl_seconds

    def _expiry(self, now: datetime) -> str:
        return (now + timedelta(seconds=self._ttl)).isoformat()

    @staticmethod
    def _is_expired(lease: Lease, now: datetime) -> bool:
        try:
            return now > datetime.fromisoformat(lease.expires_at)
        except (ValueError, TypeError):
            return True

    def acquire(self, *, profile_key: str, identity_id: str, agent_id: str, run_id: str) -> Lease:
        now = self._clock()
        with db.transaction() as conn:
            holder_data = db.get_active_lease_for_profile(conn, profile_key)
            if holder_data is not None:
                holder = Lease.from_dict(holder_data)
                if holder.run_id == run_id:
                    return holder  # idempotent re-acquire by the same run
                if not self._is_expired(holder, now):
                    raise GatewayError(
                        "BROWSER_PROFILE_BUSY",
                        message="Browser profile is held by another run",
                        details={"profile_key": profile_key, "held_by_run": holder.run_id},
                    )
                # The holder's heartbeat lapsed — reclaim it, then take the profile.
                holder.status = EXPIRED
                db.update_lease(conn, holder.to_dict())

            lease = Lease(
                binding_id=ids.new_workspace_id().replace("ws_", "bnd_"),
                profile_key=profile_key, identity_id=identity_id, agent_id=agent_id, run_id=run_id,
                status=ACTIVE, acquired_at=now.isoformat(), heartbeat_at=now.isoformat(),
                expires_at=self._expiry(now),
            )
            db.insert_lease(conn, lease.to_dict())
            return lease

    def heartbeat(self, binding_id: str) -> Lease:
        now = self._clock()
        with db.transaction() as conn:
            data = db.get_lease_in(conn, binding_id)
            if data is None:
                raise GatewayError("BROWSER_BINDING_NOT_FOUND", details={"binding_id": binding_id})
            lease = Lease.from_dict(data)
            if lease.status != ACTIVE:
                raise GatewayError("BROWSER_DISCONNECTED", details={"binding_id": binding_id, "status": lease.status})
            lease.heartbeat_at = now.isoformat()
            lease.expires_at = self._expiry(now)
            db.update_lease(conn, lease.to_dict())
            return lease

    def release(self, binding_id: str) -> None:
        with db.transaction() as conn:
            data = db.get_lease_in(conn, binding_id)
            if data is None:
                return
            lease = Lease.from_dict(data)
            if lease.status == ACTIVE:
                lease.status = RELEASED
                db.update_lease(conn, lease.to_dict())

    def reclaim_expired(self) -> list[str]:
        """Expire every active lease whose heartbeat has lapsed. Returns the reclaimed binding ids.

        A lease with a fresh heartbeat is left untouched — the live-profile guarantee (design §10.7).
        """
        now = self._clock()
        reclaimed: list[str] = []
        with db.transaction() as conn:
            for data in db.list_active_leases():
                lease = Lease.from_dict(data)
                if self._is_expired(lease, now):
                    lease.status = EXPIRED
                    db.update_lease(conn, lease.to_dict())
                    reclaimed.append(lease.binding_id)
        return reclaimed

    def get(self, binding_id: str) -> Lease | None:
        data = db.get_lease(binding_id)
        return Lease.from_dict(data) if data else None
