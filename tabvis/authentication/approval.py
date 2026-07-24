"""Approval service — first_use / always policies (design §8.5).

An approval binds to ``user + profile + exact origin + policy version`` (design §8.5). It never
authorizes a new iframe origin, cross-task session reuse, or a profile edit — it only records that a
human said yes to *this* user using *this* profile against *this* exact origin.

This is the in-memory decision + record core. The interactive prompt UI and durable storage land with
the human-in-the-loop work (design §18 item 3); here we model the policy so the Broker can ask "is a
prior approval on record?" and "does this policy require asking now?".
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict

# Bump when the approval semantics change; recorded on each grant so a stale approval can be ignored.
APPROVAL_POLICY_VERSION = 1


class ApprovalRecord(BaseModel):
    """A durable record that a human approved a (user, profile, origin) triple (design §8.5)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    credential_profile_id: str
    origin: str
    policy_version: int
    approved_by: str
    approved_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(user_id: str, profile_id: str, origin: str) -> tuple[str, str, str]:
    return (user_id, profile_id, origin)


class ApprovalService:
    """Records approvals and answers whether an authentication needs one (design §8.5)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str, str], ApprovalRecord] = {}

    def requires_approval(
        self, *, policy: str, user_id: str, credential_profile_id: str, origin: str
    ) -> bool:
        """Whether this authentication must prompt for approval now (design §8.5 table).

        * ``never``      → never prompt.
        * ``always``     → always prompt.
        * ``first_use``  → prompt unless a still-valid approval for this exact
          (user, profile, origin, current policy version) is on record.
        """
        if policy == "never":
            return False
        if policy == "always":
            return True
        if policy == "first_use":
            with self._lock:
                record = self._records.get(_key(user_id, credential_profile_id, origin))
            return not (record is not None and record.policy_version == APPROVAL_POLICY_VERSION)
        raise ValueError(f"unknown approval policy: {policy!r}")

    def record_approval(
        self, *, user_id: str, credential_profile_id: str, origin: str, approved_by: str
    ) -> ApprovalRecord:
        """Record a granted approval, bound to the exact origin and current policy version (§8.5)."""
        record = ApprovalRecord(
            id="appr_" + uuid.uuid4().hex[:16],
            user_id=user_id,
            credential_profile_id=credential_profile_id,
            origin=origin,
            policy_version=APPROVAL_POLICY_VERSION,
            approved_by=approved_by,
            approved_at=_now(),
        )
        with self._lock:
            self._records[_key(user_id, credential_profile_id, origin)] = record
        return record

    def revoke(self, *, user_id: str, credential_profile_id: str, origin: str) -> None:
        """Drop a recorded approval (user revocation / profile disable cascade)."""
        with self._lock:
            self._records.pop(_key(user_id, credential_profile_id, origin), None)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
