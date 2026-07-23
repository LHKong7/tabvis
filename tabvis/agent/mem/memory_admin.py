"""Agent Memory admin operations — inspect, edit, forget (Resume Plus §14, §18.3).

The user-facing surface for owning and controlling Agent Memory: see exactly what an agent remembers,
add or remove an explicit user preference through a structured operation (not by hand-editing the
Markdown — design open-question 4 chooses the structured path for the MVP), and forget or physically
erase an item. All of it delegates to the revisioned store, so every mutation is a new committed
revision and every forget respects the global suppression ledger.

Model-free and thin: this is the control layer a CLI/API calls; the store owns crash-safety and
suppression.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.schemas import MemorySnapshot, UserFact


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(principal_id: str, agent_id: str, *, authorize: bool = True) -> AgentMemoryStore:
    return AgentMemoryStore.open_for(principal_id, agent_id) if authorize \
        else AgentMemoryStore(principal_id, agent_id)


def inspect(principal_id: str, agent_id: str, *, authorize: bool = True) -> dict[str, Any]:
    """A redacted, human-readable view of what an agent remembers (§18.3).

    Shows the effective snapshot (suppression already applied), the current revision, and the consent
    state — never a secret (Memory holds none by construction).
    """
    store = _store(principal_id, agent_id, authorize=authorize)
    revision = store.get_current_revision()
    snap = store.get_effective_snapshot(revision)
    consent = store.get_consent()
    return {
        "agent_id": agent_id,
        "revision": revision,
        "consent": {"enabled": consent.enabled, "revoked": consent.revoked,
                    "version": consent.version, "evidence_not_before": consent.evidence_not_before},
        "user_facts": [{"id": f.id, "text": f.text, "last_confirmed_at": f.last_confirmed_at}
                       for f in snap.user_facts],
        "topics": [{"id": t.id, "title": t.title, "summary": t.summary,
                    "expires_at": t.expires_at} for t in snap.topics],
        "sessions": [{"session_id": s.session_id, "goal": s.goal, "status": s.status}
                     for s in snap.sessions],
        "tombstone_count": len(store.list_tombstones()),
    }


def add_user_fact(
    principal_id: str, agent_id: str, text: str, *, authorize: bool = True,
) -> dict[str, Any]:
    """Add (or re-confirm) an explicit user preference, committing a new revision (§8.2 edit path).

    A pinned, user-authored fact — the structured alternative to hand-editing ``user-profile.md``.
    """
    store = _store(principal_id, agent_id, authorize=authorize)
    now = _utc_now()
    for _ in range(5):  # CAS retry
        base = store.get_current_revision()
        snap = store.load_snapshot(base)
        fact = UserFact.create(text, source_refs=["user:edit"])
        existing = {f.id: f for f in snap.user_facts}
        if fact.id in existing:
            existing[fact.id].last_confirmed_at = now
            existing[fact.id].status = "active"
            facts = list(existing.values())
        else:
            fact.first_seen_at = now
            fact.last_confirmed_at = now
            facts = [*snap.user_facts, fact]
        merged = MemorySnapshot(user_facts=facts, topics=snap.topics, sessions=snap.sessions,
                                updated_at=now)
        try:
            rev = store.commit(merged, base_revision=base)
            return {"agent_id": agent_id, "fact_id": fact.id, "revision": rev}
        except Exception:  # noqa: BLE001 - MemoryConflict → reload and retry
            continue
    raise RuntimeError("could not commit user fact (memory too contended)")


def forget(
    principal_id: str, agent_id: str, target_type: str, target_id: str, *,
    reason: str = "", erase: bool = False, authorize: bool = True,
) -> dict[str, Any]:
    """Forget (logical) or erase (physical) one Memory item. Delegates to the store (§14.2)."""
    store = _store(principal_id, agent_id, authorize=authorize)
    if erase:
        ts = store.erase(target_type, target_id, reason=reason)  # type: ignore[arg-type]
    else:
        ts = store.forget(target_type, target_id, reason=reason)  # type: ignore[arg-type]
    return {"agent_id": agent_id, "target": target_id, "erased": erase, "tombstone_seq": ts.seq}


def revoke_consent(principal_id: str, agent_id: str, *, authorize: bool = True) -> dict[str, Any]:
    """Revoke Browser-Memory consent: stops new reads/writes; a rollback cannot re-enable it (§13.2)."""
    store = _store(principal_id, agent_id, authorize=authorize)
    consent = store.revoke_consent()
    return {"agent_id": agent_id, "consent_revoked": consent.revoked}
