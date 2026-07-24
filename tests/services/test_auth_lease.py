"""Cross-process authentication lease (design §13.1, §13.3, §16.2)."""

from __future__ import annotations

import pytest

from tabvis.browser import auth_lease
from tabvis.browser.auth_lease import AuthLeaseError


def test_acquire_and_release_is_exclusive() -> None:
    lease = auth_lease.acquire("sess-1", task_id="t1", request_id="r1")
    assert auth_lease.is_authentication_locked("sess-1")
    # a second acquire on the same session fails while the first is valid
    with pytest.raises(AuthLeaseError):
        auth_lease.acquire("sess-1", task_id="t2", request_id="r2")
    lease.release()
    assert not auth_lease.is_authentication_locked("sess-1")
    # now it can be acquired again
    auth_lease.acquire("sess-1", task_id="t3", request_id="r3").release()


def test_context_manager_releases() -> None:
    with auth_lease.acquire("sess-2", task_id="t1", request_id="r1"):
        assert auth_lease.is_authentication_locked("sess-2")
    assert not auth_lease.is_authentication_locked("sess-2")


def test_expired_lease_is_reclaimed_on_acquire() -> None:
    # ttl=0 → immediately expired; a new acquire reclaims it (Broker-crash recovery)
    auth_lease.acquire("sess-3", task_id="t1", request_id="r1", ttl=0.0)
    assert not auth_lease.is_authentication_locked("sess-3")
    lease2 = auth_lease.acquire("sess-3", task_id="t2", request_id="r2")
    assert lease2.lease_id
    lease2.release()


def test_any_authentication_locked() -> None:
    assert not auth_lease.any_authentication_locked()
    lease = auth_lease.acquire("sess-4", task_id="t1", request_id="r1")
    assert auth_lease.any_authentication_locked()
    lease.release()
    assert not auth_lease.any_authentication_locked()


def test_heartbeat_extends() -> None:
    lease = auth_lease.acquire("sess-5", task_id="t1", request_id="r1", ttl=0.05)
    lease.heartbeat()  # pushes expiry out by ttl
    assert auth_lease.is_authentication_locked("sess-5")
    lease.release()


def test_reclaim_expired_sweeps() -> None:
    auth_lease.acquire("sess-6", task_id="t1", request_id="r1", ttl=0.0)
    auth_lease.acquire("sess-7", task_id="t1", request_id="r1", ttl=0.0)
    live = auth_lease.acquire("sess-8", task_id="t1", request_id="r1", ttl=120.0)
    reclaimed = auth_lease.reclaim_expired()
    assert reclaimed == 2
    assert auth_lease.is_authentication_locked("sess-8")
    live.release()
