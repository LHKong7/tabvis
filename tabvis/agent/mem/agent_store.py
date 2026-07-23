"""AgentMemoryStore — the revisioned, principal+agent-scoped Memory store (design §8, §14).

Storage substrate for Resume Plus Agent Memory. It is deliberately model-free and browser-free: it
stores, revisions, and forgets structured content ( :mod:`tabvis.agent.mem.schemas` ), and enforces
the crash-safety, consent, and suppression invariants the design requires. Phase 3 (consolidation)
produces the content this commits; Phase 4 (context) reads the effective snapshot.

Guarantees:

* **Isolation** (§4.4): the on-disk path is keyed by an opaque principal scope + ``agent_id``, so
  Agent A can never address Agent B's store, and a Session ID grants nothing — the store never keys
  on it. :meth:`open_for` additionally checks the registry's owning principal before opening.
* **Crash-safe commits** (§8.2): a new revision is staged in full under its own directory, then
  ``CURRENT`` is switched with a single atomic ``os.replace``. A crash exposes either the complete
  prior or the complete next revision, never a half-updated mix.
* **CAS commits** (§10.6): a commit may require the revision it based on to still be ``CURRENT``.
* **Global suppression** (§8.2/§14.2): a ``tombstones.jsonl`` ledger sits above every revision and is
  applied on every read, so a forgotten item is neither served from an old revision nor re-exposed by
  a rollback. Physical erase additionally rewrites the item out of every stored revision.
* **Consent** (§13.2): ``consent.json`` lives outside revisions, so a rollback can never roll consent
  back or widen the allowed evidence range.

Files are ``0600`` and directories ``0700``. Writes serialize through a per-store single-writer lock
(an in-process lock plus a best-effort on-disk lease).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from tabvis.agent.mem.schemas import (
    Consent,
    Manifest,
    MemorySnapshot,
    SCHEMA_VERSION,
    Tombstone,
    TombstoneTarget,
    facts_manifest,
    render_all,
    sha256_text,
)
from tabvis.utils.env_utils import get_tabvis_config_home_dir

AGENT_MEMORY_DIRNAME = "agent-memory"
LOCAL_PRINCIPAL = "principal_local"

_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UNSET = object()

# One in-process writer lock per (scope, agent) key; the on-disk lease guards cross-process writers.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class AgentMemoryError(Exception):
    """Base for Memory-store errors (named to avoid shadowing the builtin ``MemoryError``)."""


class MemoryForbidden(AgentMemoryError):
    """The caller's principal does not own the requested agent's Memory (§4.4)."""


class MemoryConflict(AgentMemoryError):
    """A CAS commit lost the race: ``CURRENT`` changed under it (§10.6). The caller rebases."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_revision_id() -> str:
    return "memrev_" + uuid.uuid4().hex[:16]


def _validate_id(kind: str, value: str) -> str:
    if not value or not _ID_SAFE_RE.match(value) or ".." in value:
        raise AgentMemoryError(f"invalid {kind} {value!r}: expected a plain path-safe token")
    return value


def principal_scope(principal_id: str | None) -> str:
    """An opaque, validated owner scope directory — never an email or a caller-supplied path (§8.1).

    The local single-user principal maps to the fixed scope ``local``; any other principal is reduced
    to a short, stable hash so a real identifier (which may be an email) never becomes a directory
    name.
    """
    pid = (principal_id or LOCAL_PRINCIPAL).strip()
    if pid in ("", LOCAL_PRINCIPAL, "local"):
        return "local"
    return "p_" + hashlib.sha256(pid.encode("utf-8")).hexdigest()[:16]


class AgentMemoryStore:
    """A single agent's revisioned Memory namespace, scoped to its owning principal."""

    def __init__(self, principal_id: str, agent_id: str) -> None:
        self.principal_id = principal_id or LOCAL_PRINCIPAL
        self.agent_id = _validate_id("agent_id", agent_id)
        self._scope = principal_scope(self.principal_id)

    # --- opening / authorization ----------------------------------------------------------------

    @classmethod
    def open_for(cls, principal_id: str, agent_id: str) -> "AgentMemoryStore":
        """Open the store, first verifying the registry's owning principal matches (§4.4).

        A durable agent records its owner; opening its Memory under a different principal is forbidden.
        An agent with no registry record (e.g. a one-shot CLI session) is opened under the caller's
        principal — there is no other owner to contradict.
        """
        try:
            from tabvis.agent.agents import registry

            record = registry.get(agent_id)
        except Exception:  # noqa: BLE001 - registry is best-effort here
            record = None
        if record is not None:
            owner = getattr(record, "principal_id", None) or LOCAL_PRINCIPAL
            if owner != (principal_id or LOCAL_PRINCIPAL):
                raise MemoryForbidden(
                    f"agent {agent_id} is owned by a different principal; access denied."
                )
        return cls(principal_id, agent_id)

    # --- paths ----------------------------------------------------------------------------------

    @property
    def root(self) -> str:
        return os.path.join(
            get_tabvis_config_home_dir(), AGENT_MEMORY_DIRNAME, self._scope, self.agent_id
        )

    def _p(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def _revision_dir(self, revision: str) -> str:
        return self._p("revisions", _validate_id("revision", revision))

    # --- low-level fs helpers -------------------------------------------------------------------

    @staticmethod
    def _ensure_dir(path: str) -> None:
        os.makedirs(path, mode=0o700, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    def _write_file(self, path: str, text: str) -> None:
        self._ensure_dir(os.path.dirname(path))
        tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _read_file(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    # --- single-writer lock ---------------------------------------------------------------------

    def _lock_key(self) -> str:
        return f"{self._scope}/{self.agent_id}"

    def _inproc_lock(self) -> threading.RLock:
        key = self._lock_key()
        with _locks_guard:
            lock = _locks.get(key)
            if lock is None:
                lock = threading.RLock()
                _locks[key] = lock
            return lock

    class _WriterLock:
        def __init__(self, store: "AgentMemoryStore") -> None:
            self._store = store
            self._lock = store._inproc_lock()
            self._lease = store._p(".writer.lock")

        def __enter__(self) -> "AgentMemoryStore._WriterLock":
            self._lock.acquire()
            try:  # best-effort cross-process lease; the in-process lock is the real guard in one proc
                self._store._ensure_dir(os.path.dirname(self._lease))
                with open(self._lease, "w", encoding="utf-8") as fh:
                    fh.write(f"{os.getpid()}:{_utc_now()}")
            except OSError:
                pass
            return self

        def __exit__(self, *exc: object) -> None:
            try:
                os.remove(self._lease)
            except OSError:
                pass
            self._lock.release()

    def _writer(self) -> "AgentMemoryStore._WriterLock":
        return AgentMemoryStore._WriterLock(self)

    # --- CURRENT pointer ------------------------------------------------------------------------

    def get_current_revision(self) -> str | None:
        text = self._read_file(self._p("CURRENT"))
        return text.strip() if text and text.strip() else None

    def _set_current(self, revision: str) -> None:
        # A single atomic replace is the commit point — the crash-safety hinge (§8.2).
        self._write_file(self._p("CURRENT"), revision)

    # --- consent (outside revisions) ------------------------------------------------------------

    def get_consent(self) -> Consent:
        text = self._read_file(self._p("consent.json"))
        if not text:
            return Consent()
        try:
            return Consent.from_dict(json.loads(text))
        except ValueError:
            return Consent()

    def _save_consent(self, consent: Consent) -> None:
        self._write_file(self._p("consent.json"), json.dumps(consent.to_dict(), indent=2, default=str))

    def grant_consent(self, *, evidence_not_before: str | None = None) -> Consent:
        """Enable Browser Memory for this agent, recording the consent epoch (§13.2)."""
        with self._writer():
            consent = self.get_consent()
            consent.version += 1
            consent.enabled = True
            consent.revoked = False
            consent.enabled_at = _utc_now()
            consent.evidence_not_before = evidence_not_before or consent.enabled_at
            self._save_consent(consent)
            return consent

    def revoke_consent(self) -> Consent:
        """Stop new reads/writes immediately; a later rollback cannot re-enable it (§13.2)."""
        with self._writer():
            consent = self.get_consent()
            consent.enabled = False
            consent.revoked = True
            self._save_consent(consent)
            return consent

    def authorize_backfill(self, *, not_before: str) -> Consent:
        """Explicitly permit consolidating evidence older than the consent epoch (§13.2/§22.1)."""
        with self._writer():
            consent = self.get_consent()
            if not consent.enabled or consent.revoked:
                raise AgentMemoryError("cannot authorize backfill without active consent")
            consent.historical_backfill_allowed = True
            consent.backfill_not_before = not_before
            self._save_consent(consent)
            return consent

    # --- tombstones (global suppression ledger) -------------------------------------------------

    def list_tombstones(self) -> list[Tombstone]:
        text = self._read_file(self._p("tombstones.jsonl"))
        if not text:
            return []
        out: list[Tombstone] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(Tombstone.from_dict(json.loads(line)))
            except ValueError:
                continue
        return out

    def _append_tombstone(self, target_type: TombstoneTarget, target_id: str, *,
                          reason: str, erased: bool) -> Tombstone:
        existing = self.list_tombstones()
        seq = (max((t.seq for t in existing), default=0)) + 1
        ts = Tombstone(seq=seq, target_type=target_type, target_id=target_id, ts=_utc_now(),
                       reason=reason, erased=erased)
        path = self._p("tombstones.jsonl")
        self._ensure_dir(os.path.dirname(path))
        # Append is monotonic and never rewrites prior lines (§14.3).
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ts.to_dict(), default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return ts

    # --- revisions ------------------------------------------------------------------------------

    def iter_revisions(self) -> Iterator[str]:
        try:
            for name in os.listdir(self._p("revisions")):
                if os.path.isdir(self._revision_dir(name)):
                    yield name
        except OSError:
            return

    def load_snapshot(self, revision: str | None = None) -> MemorySnapshot:
        """The full (unfiltered) content of a revision, or an empty snapshot if none exists."""
        rev = revision or self.get_current_revision()
        if not rev:
            return MemorySnapshot()
        text = self._read_file(os.path.join(self._revision_dir(rev), "content.json"))
        if not text:
            return MemorySnapshot()
        try:
            return MemorySnapshot.from_json(text)
        except ValueError:
            return MemorySnapshot()

    def get_effective_snapshot(self, revision: str | None = None) -> MemorySnapshot:
        """The snapshot with the global suppression ledger applied — what reads MUST use (§8.2)."""
        return self.load_snapshot(revision).apply_tombstones(self.list_tombstones())

    def _stage_revision(self, revision: str, snapshot: MemorySnapshot) -> None:
        """Write a complete revision under its own directory (never touches CURRENT)."""
        revdir = self._revision_dir(revision)
        self._ensure_dir(revdir)
        self._write_file(os.path.join(revdir, "content.json"), snapshot.to_json())
        files = render_all(snapshot, principal=self.principal_id, agent=self.agent_id, revision=revision)
        digests: dict[str, str] = {}
        for relpath, content in files.items():
            self._write_file(os.path.join(revdir, relpath), content)
            digests[relpath] = sha256_text(content)
        manifest = Manifest(
            schema_version=SCHEMA_VERSION, principal_id=self.principal_id, agent_id=self.agent_id,
            current_revision=revision, updated_at=snapshot.updated_at or _utc_now(),
            files=digests, facts=facts_manifest(snapshot), consent=self.get_consent().to_dict(),
        )
        self._write_file(os.path.join(revdir, "manifest.json"), manifest.to_json())

    def _refresh_projections(self, snapshot: MemorySnapshot, revision: str) -> None:
        """Rewrite the top-level projection files from the *effective* snapshot (§8.2)."""
        effective = snapshot.apply_tombstones(self.list_tombstones())
        files = render_all(effective, principal=self.principal_id, agent=self.agent_id, revision=revision)
        # Clear stale per-session/topic projections, then write the current set.
        for sub in ("sessions", "topics"):
            shutil.rmtree(self._p(sub), ignore_errors=True)
        for relpath, content in files.items():
            self._write_file(self._p(relpath), content)

    def commit(self, snapshot: MemorySnapshot, *, base_revision: object = _UNSET) -> str:
        """Stage a new revision and atomically switch ``CURRENT`` to it (§8.2). Returns the new id.

        ``base_revision`` (optional) is a compare-and-swap guard: if given, the commit fails with
        :class:`MemoryConflict` unless that revision is still ``CURRENT`` — the caller then reloads,
        re-merges, and retries (§10.6). Omit it for the first commit or a deliberate force.
        """
        if snapshot.updated_at is None:
            snapshot = MemorySnapshot(snapshot.user_facts, snapshot.topics, snapshot.sessions, _utc_now())
        with self._writer():
            current = self.get_current_revision()
            if base_revision is not _UNSET and base_revision != current:
                raise MemoryConflict(
                    f"base revision {base_revision!r} is no longer CURRENT ({current!r}); rebase."
                )
            revision = _new_revision_id()
            self._stage_revision(revision, snapshot)   # complete, off to the side
            self._set_current(revision)                # atomic commit point
            self._refresh_projections(snapshot, revision)
            return revision

    def rollback(self, revision: str) -> str:
        """Point ``CURRENT`` back at an earlier revision (§8.2). Suppression still applies on read."""
        with self._writer():
            if not os.path.isdir(self._revision_dir(revision)):
                raise AgentMemoryError(f"unknown revision {revision!r}")
            self._set_current(revision)
            self._refresh_projections(self.load_snapshot(revision), revision)
            return revision

    # --- forget / erase -------------------------------------------------------------------------

    def forget(self, target_type: TombstoneTarget, target_id: str, *, reason: str = "") -> Tombstone:
        """Logical forget: append a suppression rule; the item is hidden from every read (§14.2).

        Raw evidence is untouched, so a later rebuild must respect the tombstone. The item stays in
        historical revisions (for audit) but never surfaces. Use :meth:`erase` to remove it entirely.
        """
        with self._writer():
            ts = self._append_tombstone(target_type, target_id, reason=reason, erased=False)
            current = self.get_current_revision()
            if current:
                self._refresh_projections(self.load_snapshot(current), current)
            return ts

    def erase(self, target_type: TombstoneTarget, target_id: str, *, reason: str = "") -> Tombstone:
        """Physical erase: remove the item from every stored revision AND suppress it (§14.2).

        Allowed to rewrite otherwise-immutable revisions. After this, no historical revision, manifest,
        or projection contains the item; the tombstone additionally prevents regeneration.
        """
        with self._writer():
            ts = self._append_tombstone(target_type, target_id, reason=reason, erased=True)
            ids = {target_id}
            for rev in list(self.iter_revisions()):
                snap = self.load_snapshot(rev)
                pruned = snap.without(target_type, ids)
                # Rewrite content.json + projections + manifest for this revision in place.
                revdir = self._revision_dir(rev)
                self._write_file(os.path.join(revdir, "content.json"), pruned.to_json())
                for sub in ("sessions", "topics"):
                    shutil.rmtree(os.path.join(revdir, sub), ignore_errors=True)
                files = render_all(pruned, principal=self.principal_id, agent=self.agent_id, revision=rev)
                digests: dict[str, str] = {}
                for relpath, content in files.items():
                    self._write_file(os.path.join(revdir, relpath), content)
                    digests[relpath] = sha256_text(content)
                manifest = Manifest(
                    schema_version=SCHEMA_VERSION, principal_id=self.principal_id, agent_id=self.agent_id,
                    current_revision=rev, updated_at=_utc_now(),
                    files=digests, facts=facts_manifest(pruned), consent=self.get_consent().to_dict(),
                )
                self._write_file(os.path.join(revdir, "manifest.json"), manifest.to_json())
            current = self.get_current_revision()
            if current:
                self._refresh_projections(self.load_snapshot(current), current)
            return ts

    # --- deletion -------------------------------------------------------------------------------

    def delete_all(self) -> None:
        """Remove the agent's entire Memory namespace (composite delete-agent path, §14.1)."""
        with self._writer():
            shutil.rmtree(self.root, ignore_errors=True)


def get_store(principal_id: str, agent_id: str, *, authorize: bool = True) -> AgentMemoryStore:
    """Open an :class:`AgentMemoryStore`. ``authorize`` runs the registry owner check (§4.4)."""
    if authorize:
        return AgentMemoryStore.open_for(principal_id, agent_id)
    return AgentMemoryStore(principal_id, agent_id)
