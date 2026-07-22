"""Agent registry — the structured record for every agent run, addressable by ``agent_id``.

One :class:`AgentRecord` per run, tracking identity (``agent_id`` / ``session_id``), lifecycle
(``queued → running → completed | failed | cancelled``), inputs (prompt, model, max_turns,
browser profile), progress (turns, tool calls), the outcome, and a live view of the browser that
agent is driving.

Records live in memory for the life of the process and are mirrored to disk at
``<config-home>/agents/<agent_id>.json`` so a finished run is inspectable afterwards.

Identity
--------
* ``agent_id``   — ours, stable for the whole run, and the key for every management operation
  (``GET /agents/<id>``, cancel, and which browser the agent owns).
* ``session_id`` — tabvis's own session identity, which drives the transcript path. One per run.

Cancellation is cooperative: the registry holds the ``asyncio.Task`` driving the run and cancels
it, which unwinds the agent loop and closes that agent's browser.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

# Terminal states — a record in one of these will never change again.
TERMINAL = ("completed", "failed", "cancelled")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_agent_id() -> str:
    """Short, URL-safe, collision-resistant. Prefixed so it's obvious what it is in a log."""
    return f"ag_{secrets.token_hex(4)}"


def new_run_id() -> str:
    """A fresh, unique id for one execution (Resume Plus §4.3). Distinct from the durable agent_id."""
    return f"run_{secrets.token_hex(8)}"


# The default owning principal for local single-user deployments (Resume Plus §4.4). A server/gateway
# fills a real principal; the CLI resolves this fixed one. Keeping it on the record is the seam that
# lets the resolver scope a reverse lookup by owner without a schema rewrite.
LOCAL_PRINCIPAL = "principal_local"


@dataclass
class AgentRecord:
    """Everything known about one agent run."""

    agent_id: str
    session_id: str
    status: str = "queued"  # queued | running | completed | failed | cancelled
    # --- Resume Plus identity (§4.3) ---
    # ``run_id`` distinguishes each execution of a durable agent (a reuse gets a fresh one); it is
    # append-only inspectable identity, not a lifecycle field. ``principal_id`` is the owning
    # principal the resolver scopes lookups to.
    run_id: str = ""
    principal_id: str = LOCAL_PRINCIPAL
    # --- inputs ---
    prompt: str = ""
    model: str | None = None
    max_turns: int | None = None
    profile: str | None = None  # browser profile; None => isolated per-agent
    cwd: str = ""
    # --- timing ---
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    ended_at: str | None = None
    # --- progress ---
    turns: int = 0
    tool_calls: int = 0
    # --- outcome ---
    result: str | None = None
    error: str | None = None
    is_error: bool = False
    # --- the browser this agent drives (summary; the full record is browser-session.json) ---
    browser: dict[str, Any] = field(default_factory=dict)
    # --- transport option, not part of the public record ---
    stream_partials: bool = False
    # --- per-run input, not part of the public record: replay the session's prior turns into the
    #     model (set when an existing agent is re-run via ``reuse``). ---
    resume: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("stream_partials", None)
        d.pop("resume", None)
        d["duration_ms"] = self.duration_ms()
        return d

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def duration_ms(self) -> int | None:
        if not self.started_at:
            return None
        end = self.ended_at or _utc_now()
        try:
            a = datetime.fromisoformat(self.started_at)
            b = datetime.fromisoformat(end)
        except ValueError:
            return None
        return int((b - a).total_seconds() * 1000)


# --------------------------------------------------------------------------- registry state

_records: dict[str, AgentRecord] = {}
_tasks: dict[str, asyncio.Task[Any]] = {}


def agents_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "agents")


def record_path(agent_id: str) -> str:
    return os.path.join(agents_dir(), f"{agent_id}.json")


def _write_sync(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)  # atomic — a reader never sees a half-written record


async def persist(record: AgentRecord) -> None:
    """Mirror the record to disk. Best-effort — never fail a run over bookkeeping."""
    try:
        await asyncio.to_thread(_write_sync, record_path(record.agent_id), record.to_dict())
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[AGENTS] failed to persist {record.agent_id}: {e}")
    # PERS-2: shadow the record into the SQLite metadata store. Best-effort and additive — JSON above
    # stays the source of truth, so a DB hiccup never affects the run.
    try:
        from tabvis.browser.persistence import db

        await asyncio.to_thread(db.upsert_agent, record.to_dict())
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[AGENTS] failed to mirror {record.agent_id} to sqlite: {e}")


# --------------------------------------------------------------------------- CRUD


def create(
    *,
    session_id: str = "",
    prompt: str,
    model: str | None = None,
    max_turns: int | None = None,
    profile: str | None = None,
    cwd: str = "",
    agent_id: str | None = None,
    stream_partials: bool = False,
) -> AgentRecord:
    record = AgentRecord(
        agent_id=agent_id or new_agent_id(),
        session_id=session_id,
        run_id=new_run_id(),
        prompt=prompt,
        model=model,
        max_turns=max_turns,
        profile=profile,
        cwd=cwd,
        stream_partials=stream_partials,
    )
    _records[record.agent_id] = record
    return record


def reuse(
    agent_id: str,
    *,
    prompt: str,
    model: str | None = None,
    max_turns: int | None = None,
    stream_partials: bool = False,
) -> AgentRecord | None:
    """Re-arm an EXISTING agent for another run — the reusable-agent path. None if unknown.

    An agent is a durable entity: its ``agent_id``, ``session_id`` (so the transcript continues) and
    ``profile`` (so the same bundled browser, tabs + logins, is re-attached) are **kept**; only the
    run-scoped fields (status, prompt, timings, outcome, per-run counters) are reset. ``model`` /
    ``max_turns`` may be overridden for this run; omit to keep the agent's existing values.
    """
    _ensure_loaded()
    record = _records.get(agent_id)
    if record is None:
        return None
    # Keep identity: agent_id, session_id, profile, cwd, created_at. Reset everything run-scoped.
    # A reuse is a *new execution* of the same durable agent, so it gets a fresh run_id (§4.3) —
    # that is what makes two reuses separately inspectable.
    record.status = "queued"
    record.run_id = new_run_id()
    record.prompt = prompt
    if model is not None:
        record.model = model
    if max_turns is not None:
        record.max_turns = max_turns
    record.stream_partials = stream_partials
    record.resume = True   # a reused agent replays its session's prior turns into the model
    record.started_at = None
    record.ended_at = None
    record.result = None
    record.error = None
    record.is_error = False
    record.turns = 0
    record.tool_calls = 0
    return record


def get(agent_id: str) -> AgentRecord | None:
    _ensure_loaded()
    return _records.get(agent_id)


def find_agents_by_session(
    session_id: str, *, principal_id: str | None = None
) -> list[AgentRecord]:
    """Every durable agent that claims ``session_id``, optionally scoped to an owning principal.

    The reverse of the canonical Resume mapping (§4.4): a Session belongs to exactly one Agent, so a
    well-formed store returns 0 or 1. The resolver treats >1 as ``RESUME_SESSION_AMBIGUOUS`` rather
    than guessing. When ``principal_id`` is given, records owned by a different principal are excluded
    — possession of a Session ID is not authority to operate it.
    """
    _ensure_loaded()
    out: list[AgentRecord] = []
    for record in _records.values():
        if record.session_id != session_id:
            continue
        if principal_id is not None and (record.principal_id or LOCAL_PRINCIPAL) != principal_id:
            continue
        out.append(record)
    return out


def active_run(agent_id: str) -> AgentRecord | None:
    """The agent's record if it currently has a non-terminal (queued/running) run, else None.

    The single-active-Run guard (§16.1): a caller starting a new Run for an agent that is already
    driving one must be refused (``AGENT_RUN_ACTIVE``) rather than double-driving its browser.
    """
    _ensure_loaded()
    record = _records.get(agent_id)
    if record is not None and record.status not in TERMINAL:
        return record
    return None


# --------------------------------------------------------------------------- durability (survive restarts)

_persisted_loaded = False


def _record_from_dict(data: dict[str, Any]) -> AgentRecord:
    """Rebuild an AgentRecord from its on-disk dict (ignores computed/dropped keys like duration_ms)."""
    known = {f.name for f in fields(AgentRecord)}
    record = AgentRecord(**{k: v for k, v in data.items() if k in known})
    # A record persisted as running/queued lost its driving task when the process died — it can no
    # longer be "running". Normalize to a terminal state so it is reusable and never blocks a new run.
    if record.status not in TERMINAL:
        record.status = "failed"
        if not record.error:
            record.error = "interrupted (process exited)"
    return record


def load_persisted_agents() -> int:
    """Load agent records written by earlier runs from ``<config-home>/agents/*.json`` into memory.

    This is what lets an ``agent_id`` be **reused across process restarts**: the record (with its
    session_id + profile) is restored so a later run can re-arm it. Best-effort; only fills ids not
    already live in memory. Returns how many were loaded.
    """
    directory = agents_dir()
    if not os.path.isdir(directory):
        return 0
    loaded = 0
    for name in os.listdir(directory):
        if not name.endswith(".json") or name.endswith(".tmp"):
            continue
        agent_id = name[:-len(".json")]
        if agent_id in _records:
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                _records[agent_id] = _record_from_dict(json.load(fh))
            loaded += 1
        except Exception as e:  # noqa: BLE001 - a corrupt sidecar must not break the registry
            log_for_debugging(f"[AGENTS] skipped unreadable record {name}: {e}")
    return loaded


def _load_agents_from_sqlite() -> None:
    """PERS-3: hydrate records already mirrored into the SQLite metadata store.

    Best-effort and additive: only fills ids not already live in memory, normalizing a stale
    running/queued record to a terminal state exactly like the JSON path (via ``_record_from_dict``).
    """
    try:
        from tabvis.browser.persistence import db

        for data in db.list_agents():
            agent_id = data.get("agent_id")
            if not agent_id or agent_id in _records:
                continue
            try:
                _records[agent_id] = _record_from_dict(data)
            except Exception as e:  # noqa: BLE001 - a bad row must not break the load
                log_for_debugging(f"[AGENTS] skipped unreadable sqlite row {agent_id}: {e}")
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[AGENTS] sqlite load skipped: {e}")


def _backfill_agents_to_sqlite(agent_ids: list[str]) -> None:
    """PERS-3: mirror JSON-only records into SQLite so the DB becomes complete. Idempotent."""
    if not agent_ids:
        return
    try:
        from tabvis.browser.persistence import db

        for agent_id in agent_ids:
            record = _records.get(agent_id)
            if record is not None:
                db.upsert_agent(record.to_dict())
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[AGENTS] sqlite backfill skipped: {e}")


def _ensure_loaded() -> None:
    """Load persisted records once, lazily, on first registry access.

    PERS-3: SQLite is the read authority — records already mirrored there load first — and JSON is
    both the fallback (any id the DB lacks, e.g. a record from before the DB existed) and the backfill
    source (JSON-only ids are mirrored into the DB so it becomes complete). Either store alone is
    sufficient, so a disabled or empty DB loses nothing. ``load_persisted_agents`` keeps its exact
    JSON contract untouched.
    """
    global _persisted_loaded
    if _persisted_loaded:
        return
    _persisted_loaded = True
    _load_agents_from_sqlite()
    from_db = set(_records)
    load_persisted_agents()
    _backfill_agents_to_sqlite([aid for aid in _records if aid not in from_db])


def list_agents(status: str | None = None, limit: int | None = None) -> list[AgentRecord]:
    """Newest first, optionally filtered by status."""
    _ensure_loaded()
    out = sorted(_records.values(), key=lambda r: r.created_at, reverse=True)
    if status:
        out = [r for r in out if r.status == status]
    return out[:limit] if limit else out


def running_count() -> int:
    return sum(1 for r in _records.values() if r.status == "running")


def bind_task(agent_id: str, task: asyncio.Task[Any]) -> None:
    """Hand the registry the task driving this run, so it can be cancelled by id."""
    _tasks[agent_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(agent_id, None))


async def mark_running(record: AgentRecord) -> None:
    if record.is_terminal:  # a cancel/quit landed during the start window — never revive it
        return
    record.status = "running"
    record.started_at = _utc_now()
    await persist(record)


async def mark_finished(
    record: AgentRecord,
    *,
    status: str,
    result: str | None = None,
    error: str | None = None,
    is_error: bool = False,
) -> None:
    if record.is_terminal:  # never overwrite a terminal state (e.g. cancel then completion)
        return
    record.status = status
    record.ended_at = _utc_now()
    record.result = result
    record.error = error
    record.is_error = is_error
    await persist(record)


async def cancel(agent_id: str) -> bool:
    """Cancel a running agent AND quit its bundled browser. False if unknown or already terminal.

    Cancelling an agent ends it — so its bundled browser is closed too (the agent and its browser
    are one unit). For an already-finished agent whose browser is still open, use :func:`quit`.
    """
    record = _records.get(agent_id)
    if record is None or record.is_terminal:
        return False
    task = _tasks.get(agent_id)
    if task is not None and not task.done():
        task.cancel()
    # Mark it now: the task's own cleanup may not get a chance to run before the caller replies.
    await mark_finished(record, status="cancelled", error="cancelled by request")
    await _close_agent_browser(agent_id)
    return True


async def quit(agent_id: str) -> bool:
    """Quit an agent and close its bundled browser. Works whether or not the run is still going.

    This is the "user quit them" action. A running agent is cancelled first; a finished agent that
    is merely still holding its browser open (the bundle persists past the run) has it closed and
    its profile freed. Returns False for an unknown agent.
    """
    record = _records.get(agent_id)
    if record is None:
        return False
    task = _tasks.get(agent_id)
    if task is not None and not task.done():
        task.cancel()
    if not record.is_terminal:
        await mark_finished(record, status="cancelled", error="quit by request")
    await _close_agent_browser(agent_id)
    return True


async def _close_agent_browser(agent_id: str) -> None:
    """Best-effort close of an agent's bundled browser. Never fail a quit over browser teardown."""
    try:
        from tabvis.browser.manager import quit_agent_browser

        await quit_agent_browser(agent_id)
    except Exception as e:  # noqa: BLE001 - teardown is best-effort
        log_for_debugging(f"[AGENTS] failed to close browser for {agent_id}: {e}")
