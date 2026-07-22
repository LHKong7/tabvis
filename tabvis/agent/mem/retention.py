"""Reference-aware retention for Agent Memory (Resume Plus §14.3).

Immutable revisions and a durable job queue would grow without bound, so retention prunes the old
ones — but never anything still in use. The rules the design insists on (§14.3):

* the ``CURRENT`` revision is never pruned;
* a revision referenced by a not-yet-committed job is never pruned (a running/pending consolidation
  might still read it);
* committed job records older than a keep window are pruned (their work is done and idempotent).

This bounds on-disk growth (the "bounded and observable" acceptance) without ever cutting a reference
out from under live work. Pure filesystem over the store's own directories; no model, no browser.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from tabvis.agent.mem.agent_store import AgentMemoryStore
from tabvis.agent.mem.consolidator import JobStore
from tabvis.utils.debug import log_for_debugging

_DEFAULT_KEEP_REVISIONS = 20
_DEFAULT_KEEP_JOBS = 50


@dataclass
class RetentionResult:
    pruned_revisions: list[str]
    pruned_jobs: list[str]
    kept_revisions: int


def _revision_mtime(store: AgentMemoryStore, revision: str) -> float:
    try:
        return os.path.getmtime(os.path.join(store._revision_dir(revision), "manifest.json"))  # noqa: SLF001
    except OSError:
        return 0.0


def prune_revisions(
    store: AgentMemoryStore, *, keep_last: int = _DEFAULT_KEEP_REVISIONS,
) -> list[str]:
    """Delete old immutable revisions, keeping CURRENT + the newest ``keep_last`` (reference-aware).

    A revision referenced by a not-yet-committed job is protected regardless of age.
    """
    current = store.get_current_revision()
    protected: set[str] = {current} if current else set()
    # Protect revisions a pending (uncommitted) job might still read.
    for job in JobStore(store).pending():
        if job.committed_revision:
            protected.add(job.committed_revision)

    revisions = list(store.iter_revisions())
    ranked = sorted(revisions, key=lambda r: _revision_mtime(store, r), reverse=True)
    # Always keep the newest keep_last + everything protected.
    keep = set(ranked[:keep_last]) | protected
    pruned: list[str] = []
    for rev in revisions:
        if rev in keep:
            continue
        try:
            shutil.rmtree(store._revision_dir(rev), ignore_errors=True)  # noqa: SLF001
            pruned.append(rev)
        except OSError as e:  # noqa: PERF203
            log_for_debugging(f"[MEMORY] prune revision {rev} failed: {e}")
    return pruned


def prune_committed_jobs(
    store: AgentMemoryStore, *, keep_last: int = _DEFAULT_KEEP_JOBS,
) -> list[str]:
    """Delete the oldest committed job records beyond ``keep_last``. Pending jobs are never pruned."""
    jobs_dir = store._p("jobs")  # noqa: SLF001
    try:
        names = [n for n in os.listdir(jobs_dir) if n.endswith(".json")]
    except OSError:
        return []
    store_jobs = JobStore(store)
    committed = []
    for name in names:
        job = store_jobs.get(name[:-5])
        if job is not None and job.status == "committed":
            committed.append((os.path.getmtime(os.path.join(jobs_dir, name)), name))
    committed.sort(reverse=True)  # newest first
    pruned: list[str] = []
    for _mtime, name in committed[keep_last:]:
        try:
            os.remove(os.path.join(jobs_dir, name))
            pruned.append(name[:-5])
        except OSError:
            continue
    return pruned


def sweep(
    store: AgentMemoryStore, *,
    keep_revisions: int = _DEFAULT_KEEP_REVISIONS,
    keep_jobs: int = _DEFAULT_KEEP_JOBS,
) -> RetentionResult:
    """Run the full reference-aware retention sweep for one agent. Best-effort."""
    pruned_revs = prune_revisions(store, keep_last=keep_revisions)
    pruned_jobs = prune_committed_jobs(store, keep_last=keep_jobs)
    return RetentionResult(pruned_revisions=pruned_revs, pruned_jobs=pruned_jobs,
                           kept_revisions=len(list(store.iter_revisions())))
