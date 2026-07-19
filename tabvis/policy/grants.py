"""Scoped, time-bound permission grants (PP-5).

``docs/permission-policy-engine_v1.md`` §4.1: the de-noising mechanism. When an ``ask`` is approved,
record a **grant** — a scoped, expiring ``allow`` — so the same ``(action, resource)`` does not ask
again within its scope. Grants are the highest-priority rule source (baseline < settings < identity <
grant), so a grant upgrades a baseline/settings ``ask`` to ``allow`` — but it can never override a
``deny`` (deny is absolute at the engine level), which keeps config protection ungrantable.

State lives here (a session-lifetime, in-memory store keyed by ``agent_id``, mirroring
``identity_store``). The clock is injectable (``now`` = epoch seconds) so tests are deterministic; it
defaults to the real wall clock. Rules compiled from active grants feed the engine as ``extra_rules``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from tabvis.policy.rules import PolicyRule, compile_rules

_lock = threading.Lock()
_grants: list["Grant"] = []
_counter = 0


@dataclass(frozen=True)
class Grant:
    """One scoped, expiring allow. ``agent_id=None`` means it applies to every agent (global)."""

    id: str
    action: str
    resource: str
    agent_id: str | None
    created_at: float
    expires_at: float | None  # epoch seconds; None = session-lifetime (until cleared)
    reason: str = ""

    def is_active(self, now: float, agent_id: str | None) -> bool:
        if self.expires_at is not None and now >= self.expires_at:
            return False
        if self.agent_id is not None and self.agent_id != agent_id:
            return False
        return True


def _now(now: float | None) -> float:
    return time.time() if now is None else now


def grant_pattern_for_resource(resource: str) -> str:
    """Widen a concrete resource to the pattern a grant should cover.

    A ``url:`` resource widens to its whole origin (``scheme://host[:port]/**``) so approving one page
    on a host de-noises the rest of that host — the common "I trust this site" case. Anything else is
    kept as-is (exact resource), which is the conservative default.
    """
    if not resource.startswith("url:"):
        return resource
    rest = resource[len("url:") :]
    try:
        parts = urlsplit(rest)
    except ValueError:
        return resource
    if not parts.scheme or not parts.hostname:
        return resource
    host = parts.hostname.lower()
    port = f":{parts.port}" if parts.port is not None else ""
    return f"url:{parts.scheme.lower()}://{host}{port}/**"


def add_grant(
    action: str,
    resource: str,
    *,
    agent_id: str | None = None,
    ttl_seconds: float | None = None,
    reason: str = "",
    now: float | None = None,
) -> Grant:
    """Record a grant for the exact ``(action, resource)`` given. ``ttl_seconds=None`` = session life."""
    global _counter
    ts = _now(now)
    with _lock:
        _counter += 1
        grant = Grant(
            id=f"grant-{_counter}",
            action=action,
            resource=resource,
            agent_id=agent_id,
            created_at=ts,
            expires_at=(ts + ttl_seconds) if ttl_seconds is not None else None,
            reason=reason,
        )
        _grants.append(grant)
    return grant


def record_grant_from_ask(
    action: str,
    resource: str,
    *,
    agent_id: str | None = None,
    ttl_seconds: float | None = None,
    reason: str = "",
    now: float | None = None,
) -> Grant:
    """Seam the host calls when a user approves an ``ask`` — records a grant over the widened scope.

    Uses :func:`grant_pattern_for_resource` so approving one URL on a host de-noises that host.
    """
    return add_grant(
        action,
        grant_pattern_for_resource(resource),
        agent_id=agent_id,
        ttl_seconds=ttl_seconds,
        reason=reason or "approved ask",
        now=now,
    )


def active_grants(agent_id: str | None, now: float | None = None) -> list[Grant]:
    """The grants currently in force for ``agent_id`` (unexpired, scope-matching)."""
    ts = _now(now)
    with _lock:
        return [g for g in _grants if g.is_active(ts, agent_id)]


def active_rules(agent_id: str | None, now: float | None = None) -> list[PolicyRule]:
    """Compile the active grants for ``agent_id`` into highest-priority ``allow`` rules."""
    raw = [
        {"id": g.id, "effect": "allow", "actions": [g.action], "resources": [g.resource]}
        for g in active_grants(agent_id, now)
    ]
    return compile_rules(raw)


def revoke(grant_id: str) -> bool:
    """Remove a grant by id. Returns True if one was removed."""
    with _lock:
        before = len(_grants)
        _grants[:] = [g for g in _grants if g.id != grant_id]
        return len(_grants) < before


def purge_expired(now: float | None = None) -> int:
    """Drop expired grants; returns how many were removed."""
    ts = _now(now)
    with _lock:
        before = len(_grants)
        _grants[:] = [g for g in _grants if g.expires_at is None or ts < g.expires_at]
        return before - len(_grants)


def clear() -> None:
    """Drop all grants (test isolation / session reset)."""
    global _counter
    with _lock:
        _grants.clear()
        _counter = 0
