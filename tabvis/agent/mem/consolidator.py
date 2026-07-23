"""MemoryConsolidator — turn a bounded evidence delta into committed Agent Memory (§10).

The consolidator is the write side of Agent Memory. It runs after a Run and:

1. deterministically **updates the Session Digest** for the Run (any terminal status — no model);
2. if an **extractor** is configured and the Run completed, asks it for strict JSON candidates from
   the sanitized :class:`~tabvis.agent.mem.evidence.EvidencePacket`;
3. **validates** those candidates (explicit user provenance, no secrets/instructions/cross-agent
   refs, refs within the evidence high-water) — the model proposes, deterministic code disposes;
4. **merges** them into the current :class:`~tabvis.agent.mem.agent_store.AgentMemoryStore` snapshot
   with stable ids, topic decay/expiry, and a **CAS/rebase** commit so a concurrent newer revision is
   never overwritten (§10.6).

Idempotency (§10.5): a job's id is a hash over the consent epoch, agent/session/run, the evidence
high-water fingerprint, and the sanitizer/schema/extractor versions. A job already ``committed`` is a
no-op; a retry reuses the persisted, validated CandidateSet rather than re-asking a nondeterministic
model. Everything is best-effort with respect to the Run: :func:`consolidate_run` never raises, so a
consolidation failure keeps a successful Run successful (§7.2).

The extractor is an injected seam (``Extractor``): with none configured, consolidation still updates
the deterministic Session Digest but extracts no facts/topics. This is what keeps the module testable
and the feature gated (no model wired ⇒ no model call).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from tabvis.agent.mem import sanitizer
from tabvis.agent.mem.agent_store import AgentMemoryStore, MemoryConflict
from tabvis.agent.mem.evidence import EvidenceCheckpoint, EvidencePacket
from tabvis.agent.mem.schemas import (
    SCHEMA_VERSION,
    BrowsingTopic,
    MemorySnapshot,
    SessionDigest,
    UserFact,
)
from tabvis.utils.debug import log_for_debugging

# Bumped when the extractor prompt/contract changes, so a contract change forces re-extraction (§10.5).
EXTRACTOR_VERSION = "1"
_DEFAULT_TOPIC_TTL_DAYS = 90
_MAX_STATEMENT_CHARS = 500
_MAX_SUMMARY_CHARS = 800
_MAX_CAS_ATTEMPTS = 5

# An extractor takes the packet's extractor-dict and returns raw candidate JSON (a dict). Async to
# match a real model call; a sync function can be wrapped.
Extractor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConsolidationError(Exception):
    """Extraction/validation failed for a job (kept internal — never fails a Run)."""


# --------------------------------------------------------------------------- validated candidates


@dataclass
class CandidateSet:
    """The validated, deterministic output of one extraction — persisted with the job (§10.5)."""

    user_facts: list[dict[str, Any]] = field(default_factory=list)   # {statement, source_uuid}
    topics: list[dict[str, Any]] = field(default_factory=list)       # {topic_key, summary, ...}
    session_digest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "CandidateSet":
        return CandidateSet(
            user_facts=list(d.get("user_facts", [])),
            topics=list(d.get("topics", [])),
            session_digest=dict(d.get("session_digest", {})),
        )


def validate_candidates(raw: dict[str, Any], evidence: EvidencePacket) -> CandidateSet:
    """Validate raw extractor output against schema + trust rules (§10.2). Raises on any violation."""
    if not isinstance(raw, dict):
        raise ConsolidationError("extractor output is not an object")
    allowed_top = {"sessionDigest", "userFacts", "browsingTopics"}
    unknown = set(raw) - allowed_top
    if unknown:
        raise ConsolidationError(f"unknown top-level fields: {sorted(unknown)}")

    user_uuids = evidence.user_message_uuids()
    facts: list[dict[str, Any]] = []
    for f in raw.get("userFacts", []) or []:
        if not isinstance(f, dict):
            raise ConsolidationError("userFact is not an object")
        statement = str(f.get("statement", "")).strip()
        source_uuid = f.get("sourceMessageUuid")
        # A user fact MUST be explicit and trace to a real user message (§9.3/§10.2): web content
        # and assistant claims can never mint one.
        if not statement or not f.get("explicit"):
            raise ConsolidationError("userFact requires explicit=true and a statement")
        if source_uuid not in user_uuids:
            raise ConsolidationError("userFact provenance is not a real user message")
        if len(statement) > _MAX_STATEMENT_CHARS:
            raise ConsolidationError("userFact statement too long")
        if sanitizer.classify_typed_text(statement) == "secret_like":
            raise ConsolidationError("userFact looks like a secret; rejected")
        facts.append({"statement": statement, "source_uuid": source_uuid})

    topics: list[dict[str, Any]] = []
    for t in raw.get("browsingTopics", []) or []:
        if not isinstance(t, dict):
            raise ConsolidationError("browsingTopic is not an object")
        key = str(t.get("topicKey", "")).strip()
        summary = str(t.get("summary", "")).strip()
        if not key or not summary:
            raise ConsolidationError("browsingTopic requires topicKey and summary")
        if len(summary) > _MAX_SUMMARY_CHARS:
            raise ConsolidationError("browsingTopic summary too long")
        conf = t.get("confidence", 0.7)
        if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
            raise ConsolidationError("browsingTopic confidence out of range")
        if sanitizer.classify_typed_text(summary) == "secret_like":
            raise ConsolidationError("browsingTopic summary looks like a secret; rejected")
        topics.append({
            "topic_key": key, "title": (t.get("title") or key).strip()[:120], "summary": summary,
            "confidence": float(conf), "source_refs": [str(r) for r in (t.get("sourceRefs") or [])][:20],
            "expires_at": t.get("expiresAt"),
        })

    digest = raw.get("sessionDigest") or {}
    if not isinstance(digest, dict):
        raise ConsolidationError("sessionDigest is not an object")
    return CandidateSet(user_facts=facts, topics=topics, session_digest=digest)


# --------------------------------------------------------------------------- merge


def _expired(expires_at: str | None, now: str) -> bool:
    return bool(expires_at) and expires_at < now


def merge_candidates(
    snapshot: MemorySnapshot,
    candidates: CandidateSet,
    *,
    session_id: str,
    run_id: str,
    status: str,
    now: str | None = None,
) -> MemorySnapshot:
    """Deterministically upsert candidates into ``snapshot`` (§10.3). Pure — no I/O.

    User facts upsert by stable id (newer confirmation wins, first-seen preserved); topics merge
    source refs and refresh activity, dropping expired ones; the Session Digest is replaced for this
    session. Expired topics are pruned on every merge (topic decay).
    """
    now = now or _utc_now()
    default_expiry = (datetime.fromisoformat(now.replace("Z", "+00:00"))
                      + timedelta(days=_DEFAULT_TOPIC_TTL_DAYS)).isoformat()

    facts_by_id = {f.id: f for f in snapshot.user_facts}
    for cand in candidates.user_facts:
        fact = UserFact.create(cand["statement"], source_refs=[f"transcript:{cand['source_uuid']}"])
        existing = facts_by_id.get(fact.id)
        if existing is not None:
            existing.last_confirmed_at = now
            existing.status = "active"
            if fact.source_refs[0] not in existing.source_refs:
                existing.source_refs.append(fact.source_refs[0])
        else:
            fact.first_seen_at = now
            fact.last_confirmed_at = now
            facts_by_id[fact.id] = fact

    topics_by_id = {t.id: t for t in snapshot.topics}
    for cand in candidates.topics:
        topic = BrowsingTopic.create(
            cand["topic_key"], cand["title"], cand["summary"],
            confidence=cand["confidence"], source_refs=cand["source_refs"],
            expires_at=cand.get("expires_at") or default_expiry,
        )
        existing = topics_by_id.get(topic.id)
        if existing is not None:
            existing.summary = topic.summary
            existing.confidence = max(existing.confidence, topic.confidence)
            existing.last_activity_at = now
            existing.expires_at = topic.expires_at
            for ref in topic.source_refs:
                if ref not in existing.source_refs:
                    existing.source_refs.append(ref)
        else:
            topic.first_activity_at = now
            topic.last_activity_at = now
            topics_by_id[topic.id] = topic

    # Topic decay: drop anything past its expiry.
    topics = [t for t in topics_by_id.values() if not _expired(t.expires_at, now)]

    # Session Digest: replace the current synthesis for this session (§8.6).
    digest = _build_session_digest(candidates.session_digest, session_id=session_id,
                                   run_id=run_id, status=status, now=now)
    sessions = [s for s in snapshot.sessions if s.session_id != session_id] + [digest]

    return MemorySnapshot(
        user_facts=list(facts_by_id.values()), topics=topics, sessions=sessions, updated_at=now,
    )


def _build_session_digest(raw: dict[str, Any], *, session_id: str, run_id: str,
                          status: str, now: str) -> SessionDigest:
    goal = str(raw.get("goal", "")).strip()[:_MAX_STATEMENT_CHARS]
    body_parts: list[str] = []
    for label, key in (("Confirmed conclusions", "confirmedConclusions"),
                       ("Completed", "completed"),
                       ("Open questions / next actions", "openQuestions")):
        items = [str(x).strip() for x in (raw.get(key) or []) if str(x).strip()]
        if items:
            body_parts.append(f"## {label}\n\n" + "\n".join(f"- {i[:_MAX_STATEMENT_CHARS]}" for i in items))
    return SessionDigest(session_id=session_id, goal=goal, body="\n\n".join(body_parts),
                         status=status, last_run_id=run_id, updated_at=now)


# --------------------------------------------------------------------------- jobs


def job_key(checkpoint: EvidenceCheckpoint, *, consent_version: int, evidence_not_before: str | None) -> str:
    """A deterministic job id over everything that would change the output (§10.5)."""
    basis = "\x1f".join([
        str(consent_version), evidence_not_before or "",
        checkpoint.fingerprint(),
        sanitizer.SANITIZER_VERSION, str(SCHEMA_VERSION), EXTRACTOR_VERSION,
    ])
    return "job_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


@dataclass
class ConsolidationJob:
    job_id: str
    agent_id: str
    session_id: str
    run_id: str
    status: str = "pending"  # pending | committed | failed
    candidate_set: dict[str, Any] | None = None
    committed_revision: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "ConsolidationJob":
        known = set(ConsolidationJob.__dataclass_fields__)
        return ConsolidationJob(**{k: v for k, v in d.items() if k in known})


class JobStore:
    """Durable per-agent job queue under the store's ``jobs/`` dir (§8.1). Survives restarts."""

    def __init__(self, store: AgentMemoryStore) -> None:
        self._store = store

    def _dir(self) -> str:
        return self._store._p("jobs")  # noqa: SLF001 - same package, intentional

    def _path(self, job_id: str) -> str:
        return os.path.join(self._dir(), f"{job_id}.json")

    def get(self, job_id: str) -> ConsolidationJob | None:
        try:
            with open(self._path(job_id), encoding="utf-8") as fh:
                return ConsolidationJob.from_dict(json.load(fh))
        except (OSError, ValueError):
            return None

    def save(self, job: ConsolidationJob) -> None:
        job.updated_at = _utc_now()
        d = self._dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = self._path(job.job_id)
        tmp = f"{path}.tmp.{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(job.to_dict(), fh, indent=2, default=str)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def pending(self) -> list[ConsolidationJob]:
        out: list[ConsolidationJob] = []
        try:
            names = os.listdir(self._dir())
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            job = self.get(name[:-5])
            if job is not None and job.status != "committed":
                out.append(job)
        return out


# --------------------------------------------------------------------------- orchestration


@dataclass
class ConsolidationResult:
    status: str  # committed | skipped | duplicate | failed
    job_id: str | None = None
    revision: str | None = None
    reason: str | None = None


async def consolidate_run(
    store: AgentMemoryStore,
    checkpoint: EvidenceCheckpoint,
    evidence: EvidencePacket,
    *,
    extractor: Extractor | None = None,
) -> ConsolidationResult:
    """Consolidate one Run's evidence into Memory. NEVER raises (§7.2) — a failure keeps the Run OK.

    Deterministic Session Digest update always runs (even without an extractor, and for failed /
    cancelled / interrupted Runs, §10.4). Model extraction of facts/topics runs only when an extractor
    is configured and the Run completed. Idempotent on the job key; CAS/rebase on commit.
    """
    try:
        consent = store.get_consent()
        if not consent.enabled or consent.revoked:
            return ConsolidationResult("skipped", reason="memory disabled or consent absent")

        jobs = JobStore(store)
        jid = job_key(checkpoint, consent_version=consent.version,
                      evidence_not_before=consent.evidence_not_before)
        existing = jobs.get(jid)
        if existing is not None and existing.status == "committed":
            return ConsolidationResult("duplicate", job_id=jid, revision=existing.committed_revision)

        job = existing or ConsolidationJob(job_id=jid, agent_id=store.agent_id,
                                           session_id=checkpoint.session_id, run_id=checkpoint.run_id)

        # Extraction: reuse a previously-validated CandidateSet on retry; only call the model once.
        candidates: CandidateSet
        if job.candidate_set is not None:
            candidates = CandidateSet.from_dict(job.candidate_set)
        elif extractor is not None and checkpoint.status == "completed":
            try:
                raw = await extractor(evidence.to_extractor_dict())
                candidates = validate_candidates(raw, evidence)
            except Exception as e:  # noqa: BLE001 - invalid extraction must NOT advance the checkpoint
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}"
                jobs.save(job)
                log_for_debugging(f"[MEMORY] extraction failed for {jid}: {e}")
                return ConsolidationResult("failed", job_id=jid, reason=job.error)
            job.candidate_set = candidates.to_dict()
        else:
            # No model contribution — still record the deterministic Session Digest for this Run.
            candidates = CandidateSet(session_digest={"goal": ""})

        revision = _commit_with_rebase(store, candidates, checkpoint)
        job.status = "committed"
        job.committed_revision = revision
        jobs.save(job)
        return ConsolidationResult("committed", job_id=jid, revision=revision)
    except Exception as e:  # noqa: BLE001 - the outer guard: consolidation never fails a Run
        log_for_debugging(f"[MEMORY] consolidation error (ignored): {e}")
        return ConsolidationResult("failed", reason=f"{type(e).__name__}: {e}")


def _commit_with_rebase(
    store: AgentMemoryStore, candidates: CandidateSet, checkpoint: EvidenceCheckpoint,
) -> str:
    """Merge onto the current revision and CAS-commit, rebasing if a newer revision raced in (§10.6)."""
    for _ in range(_MAX_CAS_ATTEMPTS):
        base = store.get_current_revision()
        snapshot = store.load_snapshot(base)
        merged = merge_candidates(
            snapshot, candidates, session_id=checkpoint.session_id,
            run_id=checkpoint.run_id, status=checkpoint.status,
        )
        try:
            return store.commit(merged, base_revision=base)
        except MemoryConflict:
            continue  # a newer revision landed; reload, re-merge onto it, retry
    raise ConsolidationError("exceeded CAS attempts; memory too contended")


def recover_pending_jobs(store: AgentMemoryStore) -> list[ConsolidationJob]:
    """Startup recovery: the not-yet-committed jobs for an agent (§10.4). Re-driving them is the
    caller's job; a committed job is never returned, so recovery is idempotent."""
    return JobStore(store).pending()
