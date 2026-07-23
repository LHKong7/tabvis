"""Run envelope store — the lightweight, append-only per-execution record (Resume Plus §4.3).

Every execution (legacy CLI, legacy server, or Gateway) gets a stable ``run_id`` and a small,
inspectable envelope so two reuses of the same durable agent stay distinguishable and post-Run
consolidation can be keyed idempotently. The envelope carries **bounded metadata and references
only** — never prompt bodies, DOM, tool payloads, or secrets. The Gateway's richer ``RunRecord``
remains the eventual target representation; this is the additive seam that maps onto it later.

Layout: ``<config-home>/runs/<run_id>.json`` (atomic write). A separate ``by-command/<key>`` index
records the idempotency mapping so a retried create request returns the original ``run_id`` instead
of allocating a second Run.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_TERMINAL = ("completed", "failed", "cancelled", "interrupted")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunEnvelope:
    """Bounded metadata + references for one execution (§4.3). No prompt/DOM/secret content."""

    run_id: str
    agent_id: str
    session_id: str
    principal_id: str = "principal_local"
    resume_mode: str = "fresh"  # fresh | conversation_only | plus
    command_id: str | None = None  # request/idempotency key, distinct from run_id
    input_memory_revision: str | None = None
    browser_recovery: str | None = None  # attached_live | relaunched_profile | ... (§5.2)
    status: str = "created"  # created | running | completed | failed | cancelled | interrupted
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    ended_at: str | None = None
    evidence_checkpoint_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def runs_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "runs")


def _run_path(run_id: str) -> str:
    return os.path.join(runs_dir(), f"{run_id}.json")


def _command_index_path(command_id: str) -> str:
    # A command id is caller-supplied; sanitize to a safe filename so it can never escape the dir.
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in command_id)[:128] or "cmd"
    return os.path.join(runs_dir(), "by-command", f"{safe}.txt")


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save(envelope: RunEnvelope) -> None:
    """Persist (or overwrite) an envelope. Best-effort — bookkeeping never fails a Run."""
    try:
        _write_atomic(_run_path(envelope.run_id), json.dumps(envelope.to_dict(), indent=2, default=str))
        if envelope.command_id:
            _write_atomic(_command_index_path(envelope.command_id), envelope.run_id)
    except OSError as e:
        log_for_debugging(f"[RUN] failed to persist envelope {envelope.run_id}: {e}")


def load(run_id: str) -> RunEnvelope | None:
    try:
        with open(_run_path(run_id), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    known = set(RunEnvelope.__dataclass_fields__)
    return RunEnvelope(**{k: v for k, v in data.items() if k in known})


def find_by_command(command_id: str) -> RunEnvelope | None:
    """The existing Run for an idempotency key, or None (§12.3): a retried create returns the same Run."""
    if not command_id:
        return None
    try:
        with open(_command_index_path(command_id), encoding="utf-8") as fh:
            run_id = fh.read().strip()
    except OSError:
        return None
    return load(run_id) if run_id else None


def mark_started(run_id: str) -> None:
    env = load(run_id)
    if env is None:
        return
    env.status = "running"
    env.started_at = env.started_at or _utc_now()
    save(env)


def mark_terminal(run_id: str, status: str, *, error: str | None = None,
                  evidence_checkpoint_ref: str | None = None) -> None:
    """Record a terminal outcome. Append-only in spirit: the envelope is finalized once."""
    env = load(run_id)
    if env is None:
        return
    env.status = status if status in _TERMINAL else "completed"
    env.ended_at = _utc_now()
    if error is not None:
        env.error = error
    if evidence_checkpoint_ref is not None:
        env.evidence_checkpoint_ref = evidence_checkpoint_ref
    save(env)
