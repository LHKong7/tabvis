"""Agent Memory schemas — content model, consent, manifest, tombstones, and renderers (design §8).

This is the *data* layer for the revisioned :class:`~tabvis.agent.mem.agent_store.AgentMemoryStore`.
It defines what one committed Memory revision contains (a :class:`MemorySnapshot`), the out-of-revision
:class:`Consent` and :class:`Tombstone` records, the machine :class:`Manifest`, and the deterministic
Markdown renderers that project a snapshot into the human-readable files.

Everything here is pure: no I/O, no global state. The store owns persistence, crash-safety, and the
suppression/consent policy; this module owns *shape* and *rendering*. Phase 3's extractor candidates
map onto this content model after validation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = 1

# MEMORY.md keeps the existing project-memory safety bounds (design §8.3).
MEMORY_MD_MAX_LINES = 200
MEMORY_MD_MAX_BYTES = 40_000

TombstoneTarget = Literal["fact", "topic", "session"]

_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, *, fallback: str = "item", limit: int = 96) -> str:
    """A path-safe slug for a topic/session filename (never a separator or ``..``)."""
    s = _ID_SAFE_RE.sub("-", (value or "").strip()).strip(".-")
    return (s[:limit] or fallback)


def stable_id(prefix: str, *parts: str) -> str:
    """A deterministic id from normalized parts — so re-running consolidation reuses the same id."""
    h = hashlib.sha256("\x1f".join(p.strip().lower() for p in parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- content model


@dataclass
class UserFact:
    """An explicit, user-authored preference/goal (design §8.4). Only explicit provenance allowed."""

    id: str
    text: str
    kind: str = "user_explicit"
    source_refs: list[str] = field(default_factory=list)
    confidence: float = 1.0
    first_seen_at: str | None = None
    last_confirmed_at: str | None = None
    expires_at: str | None = None
    sensitivity: str = "normal"
    status: str = "active"

    @staticmethod
    def create(text: str, *, source_refs: list[str] | None = None, **kw: Any) -> "UserFact":
        return UserFact(id=stable_id("mem", text), text=text.strip(),
                        source_refs=source_refs or [], **kw)


@dataclass
class BrowsingTopic:
    """A recent research theme (design §8.5/§8.7). Untrusted, web-derived, decays/expires."""

    id: str
    title: str
    summary: str
    aliases: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    confidence: float = 0.8
    first_activity_at: str | None = None
    last_activity_at: str | None = None
    expires_at: str | None = None

    @staticmethod
    def create(topic_key: str, title: str, summary: str, **kw: Any) -> "BrowsingTopic":
        return BrowsingTopic(id=stable_id("topic", topic_key), title=title.strip(),
                             summary=summary.strip(), **kw)


@dataclass
class SessionDigest:
    """The "where did we leave off?" synthesis for one conversation lineage (design §8.6)."""

    session_id: str
    goal: str = ""
    body: str = ""  # rendered markdown body (Phase 3 authors this from candidates)
    status: str = "completed"  # completed | failed | cancelled | interrupted
    last_run_id: str | None = None
    updated_at: str | None = None

    @property
    def id(self) -> str:  # unified handle for tombstone targeting
        return self.session_id


@dataclass
class MemorySnapshot:
    """The full structured content of one Memory revision — the source the .md files project."""

    user_facts: list[UserFact] = field(default_factory=list)
    topics: list[BrowsingTopic] = field(default_factory=list)
    sessions: list[SessionDigest] = field(default_factory=list)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": self.updated_at,
            "user_facts": [asdict(f) for f in self.user_facts],
            "topics": [asdict(t) for t in self.topics],
            "sessions": [asdict(s) for s in self.sessions],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "MemorySnapshot":
        return MemorySnapshot(
            user_facts=[UserFact(**_only(UserFact, x)) for x in d.get("user_facts", [])],
            topics=[BrowsingTopic(**_only(BrowsingTopic, x)) for x in d.get("topics", [])],
            sessions=[SessionDigest(**_only(SessionDigest, x)) for x in d.get("sessions", [])],
            updated_at=d.get("updated_at"),
        )

    @staticmethod
    def from_json(text: str) -> "MemorySnapshot":
        return MemorySnapshot.from_dict(json.loads(text))

    def without(self, target_type: TombstoneTarget, target_ids: set[str]) -> "MemorySnapshot":
        """A copy with the suppressed/erased items of ``target_type`` removed (design §14.2)."""
        return MemorySnapshot(
            user_facts=[f for f in self.user_facts
                        if not (target_type == "fact" and f.id in target_ids)],
            topics=[t for t in self.topics
                    if not (target_type == "topic" and t.id in target_ids)],
            sessions=[s for s in self.sessions
                      if not (target_type == "session" and s.session_id in target_ids)],
            updated_at=self.updated_at,
        )

    def apply_tombstones(self, tombstones: list["Tombstone"]) -> "MemorySnapshot":
        """Drop every tombstoned fact/topic/session (applied on every read — design §8.2/§14.2)."""
        by_type: dict[str, set[str]] = {"fact": set(), "topic": set(), "session": set()}
        for t in tombstones:
            by_type.setdefault(t.target_type, set()).add(t.target_id)
        snap = self
        for ttype, ids in by_type.items():
            if ids:
                snap = snap.without(ttype, ids)  # type: ignore[arg-type]
        return snap


def _only(cls: Any, d: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys that are fields of ``cls`` (tolerates forward/backward schema drift)."""
    known = set(cls.__dataclass_fields__)
    return {k: v for k, v in d.items() if k in known}


# --------------------------------------------------------------------------- consent / tombstones


@dataclass
class Consent:
    """Persisted, owner-scoped Browser-Memory consent (design §13.2). Lives OUTSIDE revisions."""

    version: int = 0
    enabled: bool = False
    enabled_at: str | None = None
    evidence_not_before: str | None = None
    historical_backfill_allowed: bool = False
    backfill_not_before: str | None = None
    revoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Consent":
        return Consent(**_only(Consent, d))

    def allows_evidence(self, ts: str | None) -> bool:
        """Whether evidence stamped ``ts`` may be consolidated under the current consent."""
        if not self.enabled or self.revoked:
            return False
        if ts is None:
            return True  # undated evidence (a live snapshot) is allowed while consent is active
        if self.evidence_not_before and ts >= self.evidence_not_before:
            return True
        # Older evidence needs an explicit historical-backfill authorization (design §13.2).
        if self.historical_backfill_allowed and self.backfill_not_before and ts >= self.backfill_not_before:
            return True
        return False


@dataclass
class Tombstone:
    """One entry in the global monotonic suppression ledger (design §8.2/§14.2)."""

    seq: int
    target_type: TombstoneTarget
    target_id: str
    ts: str
    reason: str = ""
    erased: bool = False  # True once the item was also physically removed from revisions

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Tombstone":
        return Tombstone(**_only(Tombstone, d))


# --------------------------------------------------------------------------- manifest


@dataclass
class Manifest:
    """Machine metadata for a committed revision (design §8.8). No secret content."""

    schema_version: int
    principal_id: str
    agent_id: str
    current_revision: str
    updated_at: str | None
    files: dict[str, str] = field(default_factory=dict)   # relpath -> sha256
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    consent: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    @staticmethod
    def from_json(text: str) -> "Manifest":
        return Manifest(**_only(Manifest, json.loads(text)))


# --------------------------------------------------------------------------- renderers


_FRONTMATTER = (
    "---\n"
    "schema_version: {schema}\n"
    "kind: {kind}\n"
    "principal_id: {principal}\n"
    "agent_id: {agent}\n"
    "memory_revision: {revision}\n"
    "updated_at: {updated}\n"
    "{extra}"
    "---\n\n"
)


def _fm(kind: str, principal: str, agent: str, revision: str, updated: str | None,
        extra: dict[str, str] | None = None) -> str:
    extra_lines = "".join(f"{k}: {v}\n" for k, v in (extra or {}).items())
    return _FRONTMATTER.format(schema=SCHEMA_VERSION, kind=kind, principal=principal, agent=agent,
                               revision=revision, updated=updated or "", extra=extra_lines)


def render_user_profile(snap: MemorySnapshot, *, principal: str, agent: str, revision: str) -> str:
    body = ["# User Profile", "", "## Explicit preferences and goals", ""]
    facts = [f for f in snap.user_facts if f.status == "active"]
    if facts:
        body += [f"- {f.text}" for f in facts]
    else:
        body.append("_None recorded._")
    return _fm("user_profile", principal, agent, revision, snap.updated_at) + "\n".join(body) + "\n"


def render_browsing_profile(snap: MemorySnapshot, *, principal: str, agent: str, revision: str) -> str:
    body = ["# Browsing Profile", "", "## Active research", ""]
    if snap.topics:
        for t in snap.topics:
            body.append(f"- {t.title} — {t.summary}")
    else:
        body.append("_None recorded._")
    return _fm("browsing_profile", principal, agent, revision, snap.updated_at) + "\n".join(body) + "\n"


def render_session_digest(digest: SessionDigest, *, principal: str, agent: str, revision: str) -> str:
    extra = {"session_id": digest.session_id, "status": digest.status}
    if digest.last_run_id:
        extra["last_run_id"] = digest.last_run_id
    body = ["# Session Digest", "", "## Goal", "", digest.goal or "_(unstated)_", ""]
    if digest.body.strip():
        body += [digest.body.strip(), ""]
    return _fm("session_digest", principal, agent, revision, digest.updated_at, extra) + "\n".join(body) + "\n"


def render_topic(topic: BrowsingTopic, *, principal: str, agent: str, revision: str) -> str:
    extra = {"topic_id": topic.id}
    body = [f"# {topic.title}", "", topic.summary or "", ""]
    if topic.aliases:
        body += ["## Aliases", "", ", ".join(topic.aliases), ""]
    return _fm("topic", principal, agent, revision, topic.last_activity_at, extra) + "\n".join(body) + "\n"


def render_memory_index(snap: MemorySnapshot) -> str:
    """The ``MEMORY.md`` entry index, bounded to 200 lines / 40 KB (design §8.3)."""
    lines = ["# Agent memory", ""]
    if snap.user_facts:
        lines.append("- [User profile](user-profile.md) — Explicit preferences and durable goals.")
    if snap.topics:
        lines.append("- [Current browsing profile](browsing-profile.md) — Active research themes.")
    for s in snap.sessions:
        goal = (s.goal or "session").splitlines()[0][:80]
        lines.append(f"- [Session {s.session_id}](sessions/{_slug(s.session_id)}.md) — {goal}")
    for t in snap.topics:
        summary = (t.summary or t.title).splitlines()[0][:80]
        lines.append(f"- [{t.title}](topics/{_slug(t.id)}.md) — {summary}")
    return _bound_markdown("\n".join(lines) + "\n")


def _bound_markdown(text: str) -> str:
    """Enforce the MEMORY.md line and byte caps, truncating with a marker if needed."""
    lines = text.splitlines()
    if len(lines) > MEMORY_MD_MAX_LINES:
        lines = lines[: MEMORY_MD_MAX_LINES - 1] + ["- … (truncated)"]
    out = "\n".join(lines) + "\n"
    data = out.encode("utf-8")
    if len(data) > MEMORY_MD_MAX_BYTES:
        out = data[: MEMORY_MD_MAX_BYTES - 16].decode("utf-8", "ignore") + "\n… (truncated)\n"
    return out


def session_relpath(session_id: str) -> str:
    return f"sessions/{_slug(session_id)}.md"


def topic_relpath(topic_id: str) -> str:
    return f"topics/{_slug(topic_id)}.md"


def render_all(snap: MemorySnapshot, *, principal: str, agent: str, revision: str) -> dict[str, str]:
    """Every projection file for a snapshot: relpath -> rendered Markdown."""
    files: dict[str, str] = {
        "MEMORY.md": render_memory_index(snap),
        "user-profile.md": render_user_profile(snap, principal=principal, agent=agent, revision=revision),
        "browsing-profile.md": render_browsing_profile(snap, principal=principal, agent=agent, revision=revision),
    }
    for s in snap.sessions:
        files[session_relpath(s.session_id)] = render_session_digest(
            s, principal=principal, agent=agent, revision=revision)
    for t in snap.topics:
        files[topic_relpath(t.id)] = render_topic(t, principal=principal, agent=agent, revision=revision)
    return files


def facts_manifest(snap: MemorySnapshot) -> dict[str, dict[str, Any]]:
    """The per-fact metadata block for the manifest (source refs, confidence, expiry — no content)."""
    out: dict[str, dict[str, Any]] = {}
    for f in snap.user_facts:
        out[f.id] = {"kind": f.kind, "sourceRefs": f.source_refs, "confidence": f.confidence,
                     "expiresAt": f.expires_at, "sensitivity": f.sensitivity, "status": f.status}
    for t in snap.topics:
        out[t.id] = {"kind": "browsing_topic", "sourceRefs": t.source_refs,
                     "confidence": t.confidence, "expiresAt": t.expires_at, "status": "active"}
    return out
