"""Cross-process browser authentication lease (design §13.1, §4.2).

While an authentication is in flight the browser session is held under an exclusive lease. Unlike the
existing in-process workspace ownership (``manager.py``), this lease is a **file on disk** so it works
across the process split: the Broker/Executor lives in one process and the Browser Host in another, and
either can observe the lease. Guarantees (§13.1):

* acquired atomically (``O_CREAT | O_EXCL``) — two holders can never co-exist;
* carries a hard expiry and a heartbeat; a Broker crash leaves a lease that the Browser Host reclaims
  once it goes stale (§13.3);
* while a valid lease exists, every *ordinary* Agent browser RPC is refused with
  ``browser_authentication_locked`` (wired in :mod:`tabvis.browser.policy_guard`).

The lease file holds only non-secret coordination fields (ids, pid, timestamps) — never a capability
or secret (§5.6 "MUST NOT 返回 ... 日志").
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from tabvis.utils.env_utils import get_tabvis_config_home_dir

_LEASES_DIRNAME = "browser-auth-leases"
_DEFAULT_TTL_SECONDS = 120.0  # hard expiry; refreshed by heartbeat during a live authentication
_lock = threading.RLock()


class AuthLeaseError(RuntimeError):
    """Raised when a lease cannot be acquired because another valid lease is held."""


def _leases_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), _LEASES_DIRNAME)


def _path(browser_session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in browser_session_id)[:128]
    return os.path.join(_leases_dir(), f"{safe or 'session'}.lease")


def _now() -> float:
    return time.time()


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _expired(record: dict, *, now: float | None = None) -> bool:
    return (now or _now()) >= float(record.get("expires_at", 0))


class AuthLease:
    """A held lease handle. Use :meth:`heartbeat` to extend and :meth:`release` (or a ``with`` block)."""

    def __init__(self, lease_id: str, browser_session_id: str, path: str, ttl: float) -> None:
        self.lease_id = lease_id
        self.browser_session_id = browser_session_id
        self._path = path
        self._ttl = ttl
        self._released = False

    def heartbeat(self) -> bool:
        """Extend this lease, returning whether it is still owned by this handle."""
        if self._released:
            return False
        with _session_lock(self._path):
            record = _read(self._path)
            if record is None or record.get("lease_id") != self.lease_id:
                return False  # our lease is gone (reclaimed) — nothing to extend
            record["heartbeat_at"] = _now()
            record["expires_at"] = _now() + self._ttl
            _write_atomic(self._path, record)
            return True

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _session_lock(self._path):
            record = _read(self._path)
            if record is not None and record.get("lease_id") == self.lease_id:
                try:
                    os.remove(self._path)
                except OSError:
                    pass

    def __enter__(self) -> "AuthLease":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    @property
    def heartbeat_interval(self) -> float:
        """Safe automatic-renewal interval used by the Browser Host."""
        return max(0.1, self._ttl / 3.0)


@contextmanager
def _session_lock(path: str) -> Iterator[None]:
    """Serialize lease read/modify/write across processes.

    ``threading.RLock`` only protects callers in this interpreter.  The sibling lock file closes the
    stale-reclaim/heartbeat race between the Browser Host and Broker processes.  Unix-domain Broker
    deployments run on platforms with ``fcntl``; the import fallback retains the prior in-process
    behavior on unsupported development platforms.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lock_path = f"{path}.lock"
    with _lock:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            os.close(fd)


def _write_atomic(path: str, record: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh)
    os.replace(tmp, path)


def acquire(browser_session_id: str, *, task_id: str, request_id: str, ttl: float = _DEFAULT_TTL_SECONDS) -> AuthLease:
    """Atomically acquire the authentication lease for a session, reclaiming a stale one (§13.1).

    Raises :class:`AuthLeaseError` if a *valid* (non-expired) lease is already held.
    """
    os.makedirs(_leases_dir(), exist_ok=True)
    path = _path(browser_session_id)
    lease_id = "lease_" + uuid.uuid4().hex[:16]
    record = {
        "lease_id": lease_id,
        "browser_session_id": browser_session_id,
        "task_id": task_id,
        "request_id": request_id,
        "holder_pid": os.getpid(),
        "acquired_at": _now(),
        "heartbeat_at": _now(),
        "expires_at": _now() + ttl,
    }
    with _session_lock(path):
        # Reclaim a stale lease first (§13.3 Broker-crash recovery).
        existing = _read(path)
        if existing is not None and not _expired(existing):
            raise AuthLeaseError("browser_authentication_locked")
        if existing is not None:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise AuthLeaseError("browser_authentication_locked") from None
        try:
            os.write(fd, json.dumps(record).encode("utf-8"))
        finally:
            os.close(fd)
    return AuthLease(lease_id, browser_session_id, path, ttl)


def is_authentication_locked(browser_session_id: str) -> bool:
    """Whether a valid authentication lease is currently held for this session (§4.2)."""
    path = _path(browser_session_id)
    with _session_lock(path):
        record = _read(path)
        return record is not None and not _expired(record)


def any_authentication_locked() -> bool:
    """Whether *any* session currently holds a valid authentication lease.

    Ordinary browser tools consult this to refuse observation/interaction during authentication, even
    when the tool call has no explicit session id (§4.2, §13.1).
    """
    directory = _leases_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    now = _now()
    for name in names:
        if not name.endswith(".lease"):
            continue
        path = os.path.join(directory, name)
        with _session_lock(path):
            record = _read(path)
            if record is not None and not _expired(record, now=now):
                return True
    return False


def reclaim_expired() -> int:
    """Remove every expired lease file; returns how many were reclaimed (Browser Host sweep, §13.3)."""
    directory = _leases_dir()
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    now = _now()
    reclaimed = 0
    for name in names:
        if not name.endswith(".lease"):
            continue
        path = os.path.join(directory, name)
        with _session_lock(path):
            record = _read(path)
            if record is None or _expired(record, now=now):
                try:
                    os.remove(path)
                    reclaimed += 1
                except OSError:
                    pass
    return reclaimed
