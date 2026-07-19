"""Workflow agent-result **journal** — the persistence behind pause/resume (PRD G8).

A workflow can spawn hundreds of sub-agents; if a run is killed or crashes partway, re-running it
from scratch wastes all the work already done. The journal records each completed sub-agent's input
spec + result, in call order, to a small JSONL file. On **resume** the runner replays the journaled
results for the matching prefix of ``agent()`` calls — returning the cached result instead of
re-spawning the sub-agent — and only runs live once a call's spec diverges from (or runs past) the
journal. Same script + same inputs → a 100% replay; an edited script re-runs from the first changed
``agent()`` call.

The journal is index-addressed: entry ``i`` corresponds to the ``i``-th ``agent()`` call in the run.
Each entry stores a stable hash of the call's input spec so a divergent call (different prompt/opts)
is detected and the journal is truncated at that point. Journals live under the session's task-output
directory (ephemeral, project-scoped, auto-readable) keyed by ``task_id``.

Casing: Python identifiers are snake_case; persisted entry keys (``specHash`` / ``result``) and the
nested ``WorkflowAgentResult`` keep their wire keys verbatim (they round-trip through JSON).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from tabvis.utils.task.disk_output import get_task_output_dir

__all__ = [
    "JournalEntry",
    "append_journal_entry",
    "clear_journal",
    "journal_path",
    "load_journal",
    "spec_hash",
]

JournalEntry = dict[str, Any]  # {"specHash": str, "result": WorkflowAgentResult}


def spec_hash(spec: Any) -> str:
    """A stable content hash of an ``agent()`` input spec (order-independent, JSON-based)."""
    encoded = json.dumps(spec, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def journal_path(task_id: str) -> str:
    """Path to the journal file for ``task_id`` (under the session task-output dir)."""
    return os.path.join(get_task_output_dir(), f"{task_id}.journal.jsonl")


def load_journal(task_id: str) -> list[JournalEntry]:
    """Read a journal back into a list of entries (empty if none / unreadable)."""
    path = journal_path(task_id)
    if not os.path.isfile(path):
        return []
    entries: list[JournalEntry] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    break  # stop at the first corrupt line (a partial write) — keep the good prefix
                if isinstance(entry, dict) and "specHash" in entry and "result" in entry:
                    entries.append(entry)
    except OSError:
        return []
    return entries


def write_journal(task_id: str, entries: list[JournalEntry]) -> None:
    """Atomically rewrite the journal to exactly ``entries`` (used to truncate on divergence)."""
    path = journal_path(task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_journal_entry(task_id: str, entry: JournalEntry) -> None:
    """Append one completed-agent entry to the journal."""
    path = journal_path(task_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def clear_journal(task_id: str) -> None:
    """Remove the journal for ``task_id`` (a fresh, non-resumed run starts clean)."""
    path = journal_path(task_id)
    try:
        os.remove(path)
    except OSError:
        pass
