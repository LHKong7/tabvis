"""SQLite ``gateway.db`` — the authoritative gateway store (design §12).

Holds the aggregates and the append-only log the control plane depends on: ``runs``, the durable
``events`` log, its ``outbox`` sibling, and the ``commands`` idempotency ledger. It sits beside the
existing ``runtime.db`` shadow but is a **separate, authoritative** database (see the package
docstring) so the two never contend on one connection and their durability contracts stay distinct.

Ordering guarantees the log provides (design §5.5):

* ``events.cursor`` is ``INTEGER PRIMARY KEY AUTOINCREMENT`` → a globally monotonic position.
* ``UNIQUE(aggregate_type, aggregate_id, seq)`` → a strictly increasing per-aggregate sequence.

The §12.3 transaction boundary (compare-and-set state · insert command receipt · insert event +
outbox, all-or-nothing) is provided by :func:`transaction`. tabvis is single-process; every access is
serialised under a re-entrant lock and the connection is ``check_same_thread=False`` so an async
caller can reach it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from tabvis.browser.persistence.paths import get_browser_os_data_dir

SCHEMA_VERSION = 4
GATEWAY_DB_FILENAME = "gateway.db"

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

_DDL = (
    """CREATE TABLE IF NOT EXISTS runs (
        run_id     TEXT PRIMARY KEY,
        agent_id   TEXT NOT NULL,
        session_id TEXT,
        command_id TEXT,
        status     TEXT NOT NULL,
        created_at TEXT,
        ended_at   TEXT,
        data       TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runs_agent ON runs(agent_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id)",
    # The durable event log. cursor is the global monotonic position (AUTOINCREMENT never reuses a
    # value, even across deletes); the composite UNIQUE enforces the per-aggregate sequence.
    """CREATE TABLE IF NOT EXISTS events (
        cursor         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id       TEXT NOT NULL UNIQUE,
        aggregate_type TEXT NOT NULL,
        aggregate_id   TEXT NOT NULL,
        seq            INTEGER NOT NULL,
        type           TEXT NOT NULL,
        occurred_at    TEXT,
        correlation_id TEXT,
        causation_id   TEXT,
        data           TEXT NOT NULL,
        UNIQUE(aggregate_type, aggregate_id, seq)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_aggregate ON events(aggregate_type, aggregate_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_events_run ON events(aggregate_id, cursor)",
    # Outbox: one row per event awaiting fan-out (design §1.5, §5.3). Inserted in the same tx as the
    # event, drained by the publisher, so a crash between commit and publish loses nothing.
    """CREATE TABLE IF NOT EXISTS outbox (
        cursor     INTEGER PRIMARY KEY,
        event_id   TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        attempts   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox(status, cursor)",
    # Idempotency ledger: a command_id maps to exactly one stored result (design §3.1, §5.5).
    """CREATE TABLE IF NOT EXISTS commands (
        command_id TEXT PRIMARY KEY,
        type       TEXT,
        result     TEXT NOT NULL,
        created_at TEXT
    )""",
    # v2: pending questions/approvals a Run is blocked on (design §5.2, §12.2). Durable so a restart
    # can reconstruct what a Run was waiting for.
    """CREATE TABLE IF NOT EXISTS interactions (
        interaction_id      TEXT PRIMARY KEY,
        run_id              TEXT NOT NULL,
        agent_id            TEXT,
        session_id          TEXT,
        kind                TEXT NOT NULL,
        status              TEXT NOT NULL,
        created_at          TEXT,
        expires_at          TEXT,
        answered_at         TEXT,
        response_command_id TEXT,
        data                TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_interactions_run ON interactions(run_id, status)",
    # v3: Channel Framework (design §4.3, §12.2). A ChannelAccount is one configured external
    # connection; a ConversationBinding maps an external thread to an internal conversation.
    """CREATE TABLE IF NOT EXISTS channel_accounts (
        channel_account_id  TEXT PRIMARY KEY,
        plugin_id           TEXT NOT NULL,
        tenant_id           TEXT,
        external_account_ref TEXT,
        status              TEXT NOT NULL,
        data                TEXT NOT NULL
    )""",
    # The UNIQUE key is the design's guard (§4.3): one external thread never maps to two internal
    # conversations, even under webhook retries.
    """CREATE TABLE IF NOT EXISTS conversation_bindings (
        binding_id               TEXT PRIMARY KEY,
        channel_account_id       TEXT NOT NULL,
        external_conversation_id TEXT NOT NULL,
        conversation_id          TEXT NOT NULL,
        session_id               TEXT,
        agent_id                 TEXT,
        data                     TEXT NOT NULL,
        UNIQUE(channel_account_id, external_conversation_id)
    )""",
    # Inbound dedupe ledger: an external_event_id is processed at most once (design §4.5, §5.5).
    """CREATE TABLE IF NOT EXISTS channel_inbound (
        channel_account_id TEXT NOT NULL,
        external_event_id  TEXT NOT NULL,
        conversation_id    TEXT,
        run_id             TEXT,
        message_id         TEXT,
        created_at         TEXT,
        PRIMARY KEY (channel_account_id, external_event_id)
    )""",
    # Outbound delivery receipts, idempotent on delivery_id (design §4.5, §12.2).
    """CREATE TABLE IF NOT EXISTS deliveries (
        delivery_id        TEXT PRIMARY KEY,
        channel_account_id TEXT,
        run_id             TEXT,
        status             TEXT NOT NULL,
        created_at         TEXT,
        data               TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bindings_account ON conversation_bindings(channel_account_id)",
    "CREATE INDEX IF NOT EXISTS idx_deliveries_run ON deliveries(run_id)",
    # v4: durable browser leases (design §10.5, §10.7). The lease is the exclusive claim on a profile;
    # it must survive a crash so recovery never silently reassigns a profile a live run still holds.
    """CREATE TABLE IF NOT EXISTS browser_leases (
        binding_id   TEXT PRIMARY KEY,
        profile_key  TEXT NOT NULL,
        identity_id  TEXT,
        agent_id     TEXT,
        run_id       TEXT,
        status       TEXT NOT NULL,
        acquired_at  TEXT,
        heartbeat_at TEXT,
        expires_at   TEXT,
        data         TEXT NOT NULL
    )""",
    # At most one ACTIVE lease per profile — the atomic "one active writer" guard (design §10.5).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_lease ON browser_leases(profile_key) WHERE status='active'",
    "CREATE INDEX IF NOT EXISTS idx_leases_status ON browser_leases(status)",
)


def gateway_db_path() -> str:
    return os.path.join(get_browser_os_data_dir(), GATEWAY_DB_FILENAME)


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    for stmt in _DDL:
        conn.execute(stmt)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def connect() -> sqlite3.Connection:
    """Open (or reuse) the connection for the current config home; reopen if the path changed.

    Reopening on a path change is what lets each test's tmp ``config_home`` get a fresh database
    without a manual teardown, mirroring the shadow store's behaviour.
    """
    global _conn, _conn_path
    path = gateway_db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:  # noqa: BLE001
            pass
        _conn = None
    os.makedirs(get_browser_os_data_dir(), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    _conn, _conn_path = conn, path
    return _conn


def close() -> None:
    """Close the connection (tests / shutdown). Safe to call repeatedly."""
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn, _conn_path = None, None


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """A serialised, all-or-nothing unit of work (design §12.3 transaction boundary).

    All statements inside the block commit together or roll back together. The lock makes the whole
    block atomic against other callers in this single-process daemon. Errors propagate — this store is
    authoritative, not best-effort.
    """
    with _lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- runs


def insert_run(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, agent_id, session_id, command_id, status, created_at, ended_at, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["run_id"],
            record["agent_id"],
            record.get("session_id"),
            record.get("command_id"),
            record["status"],
            record.get("created_at"),
            record.get("ended_at"),
            json.dumps(record, default=str),
        ),
    )


def update_run(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET status=?, ended_at=?, data=? WHERE run_id=?",
        (record["status"], record.get("ended_at"), json.dumps(record, default=str), record["run_id"]),
    )


def get_run(run_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def get_run_in(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    """Load a run within an open transaction (sees that transaction's uncommitted writes)."""
    row = conn.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def get_run_status(conn: sqlite3.Connection, run_id: str) -> str | None:
    row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return row["status"] if row else None


def list_runs_for_agent(agent_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = connect()
        sql = "SELECT data FROM runs WHERE agent_id = ? ORDER BY created_at DESC, rowid DESC"
        params: tuple[Any, ...] = (agent_id,)
        if limit:
            sql += " LIMIT ?"
            params = (agent_id, limit)
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(r["data"]) for r in rows]


def count_active_runs_for_agent(conn: sqlite3.Connection, agent_id: str, active_states: tuple[str, ...]) -> int:
    placeholders = ",".join("?" for _ in active_states)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM runs WHERE agent_id = ? AND status IN ({placeholders})",
        (agent_id, *active_states),
    ).fetchone()
    return int(row["n"])


def count_active_runs(active_states: tuple[str, ...]) -> int:
    """Global count of non-terminal runs — feeds gateway capacity reporting (design §2.3)."""
    placeholders = ",".join("?" for _ in active_states)
    with _lock:
        conn = connect()
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM runs WHERE status IN ({placeholders})", tuple(active_states)
        ).fetchone()
    return int(row["n"])


def get_run_by_command(command_id: str) -> dict[str, Any] | None:
    """The run created by ``command_id``, if any — the domain-level idempotency key for run.create."""
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT data FROM runs WHERE command_id = ? ORDER BY created_at ASC LIMIT 1", (command_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


# --------------------------------------------------------------------------- events / outbox


def next_seq(conn: sqlite3.Connection, aggregate_type: str, aggregate_id: str) -> int:
    """The next per-aggregate sequence number (1-based, strictly increasing)."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE aggregate_type = ? AND aggregate_id = ?",
        (aggregate_type, aggregate_id),
    ).fetchone()
    return int(row["m"]) + 1


def insert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    seq: int,
    type: str,
    occurred_at: str,
    correlation_id: str | None,
    causation_id: str | None,
    envelope: dict[str, Any],
    created_at: str,
) -> int:
    """Append one event and its outbox row; returns the assigned global ``cursor``."""
    cur = conn.execute(
        "INSERT INTO events (event_id, aggregate_type, aggregate_id, seq, type, occurred_at, "
        "correlation_id, causation_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            aggregate_type,
            aggregate_id,
            seq,
            type,
            occurred_at,
            correlation_id,
            causation_id,
            json.dumps(envelope, default=str),
        ),
    )
    cursor = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO outbox (cursor, event_id, status, attempts, created_at) VALUES (?, ?, 'pending', 0, ?)",
        (cursor, event_id, created_at),
    )
    return cursor


def read_events(
    *,
    after_cursor: int = 0,
    aggregate_id: str | None = None,
    aggregate_type: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Durable replay: every event with ``cursor > after_cursor``, in cursor order (design §5.3).

    Optionally filtered to one aggregate (e.g. a single run's stream). This is the resumable-subscribe
    read: a client passes its last cursor and receives everything after it, with no gap or duplicate.
    """
    clauses = ["cursor > ?"]
    params: list[Any] = [after_cursor]
    if aggregate_id is not None:
        clauses.append("aggregate_id = ?")
        params.append(aggregate_id)
    if aggregate_type is not None:
        clauses.append("aggregate_type = ?")
        params.append(aggregate_type)
    sql = f"SELECT data FROM events WHERE {' AND '.join(clauses)} ORDER BY cursor ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with _lock:
        conn = connect()
        rows = conn.execute(sql, params).fetchall()
    return [json.loads(r["data"]) for r in rows]


def latest_cursor() -> int:
    """The highest assigned cursor, or 0 if the log is empty."""
    with _lock:
        conn = connect()
        row = conn.execute("SELECT COALESCE(MAX(cursor), 0) AS m FROM events").fetchone()
    return int(row["m"])


def pending_outbox(limit: int = 100) -> list[dict[str, Any]]:
    """Undelivered events, oldest first — the publisher's work queue (design §1.5)."""
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT o.cursor, o.event_id, o.attempts, e.data FROM outbox o "
            "JOIN events e ON e.cursor = o.cursor WHERE o.status = 'pending' ORDER BY o.cursor ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {"cursor": r["cursor"], "event_id": r["event_id"], "attempts": r["attempts"], "envelope": json.loads(r["data"])}
        for r in rows
    ]


def mark_outbox_delivered(cursor: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE outbox SET status = 'delivered' WHERE cursor = ?", (cursor,))


def bump_outbox_attempt(cursor: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE cursor = ?", (cursor,))


# --------------------------------------------------------------------------- commands (idempotency)


def get_command_result(command_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute("SELECT result FROM commands WHERE command_id = ?", (command_id,)).fetchone()
    return json.loads(row["result"]) if row else None


def insert_command_result(conn: sqlite3.Connection, command_id: str, ctype: str, result: dict[str, Any], created_at: str) -> None:
    conn.execute(
        "INSERT INTO commands (command_id, type, result, created_at) VALUES (?, ?, ?, ?)",
        (command_id, ctype, json.dumps(result, default=str), created_at),
    )


# --------------------------------------------------------------------------- interactions


def insert_interaction(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO interactions (interaction_id, run_id, agent_id, session_id, kind, status, "
        "created_at, expires_at, answered_at, response_command_id, data) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["interaction_id"],
            record["run_id"],
            record.get("agent_id"),
            record.get("session_id"),
            record["kind"],
            record["status"],
            record.get("created_at"),
            record.get("expires_at"),
            record.get("answered_at"),
            record.get("response_command_id"),
            json.dumps(record, default=str),
        ),
    )


def update_interaction(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE interactions SET status=?, answered_at=?, response_command_id=?, data=? "
        "WHERE interaction_id=?",
        (
            record["status"],
            record.get("answered_at"),
            record.get("response_command_id"),
            json.dumps(record, default=str),
            record["interaction_id"],
        ),
    )


def get_interaction(interaction_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT data FROM interactions WHERE interaction_id = ?", (interaction_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


def get_interaction_in(conn: sqlite3.Connection, interaction_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data FROM interactions WHERE interaction_id = ?", (interaction_id,)
    ).fetchone()
    return json.loads(row["data"]) if row else None


def find_pending_interaction_for_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data FROM interactions WHERE run_id = ? AND status = 'pending' "
        "ORDER BY created_at ASC LIMIT 1",
        (run_id,),
    ).fetchone()
    return json.loads(row["data"]) if row else None


def list_pending_interactions() -> list[dict[str, Any]]:
    """Every still-pending interaction — the set a restart must reconstruct (design §5.2)."""
    with _lock:
        conn = connect()
        rows = conn.execute(
            "SELECT data FROM interactions WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
    return [json.loads(r["data"]) for r in rows]


# --------------------------------------------------------------------------- channel accounts


def upsert_channel_account(record: dict[str, Any]) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO channel_accounts (channel_account_id, plugin_id, tenant_id, external_account_ref, status, data) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(channel_account_id) DO UPDATE SET plugin_id=excluded.plugin_id, "
            "tenant_id=excluded.tenant_id, external_account_ref=excluded.external_account_ref, "
            "status=excluded.status, data=excluded.data",
            (
                record["channel_account_id"],
                record["plugin_id"],
                record.get("tenant_id"),
                record.get("external_account_ref"),
                record["status"],
                json.dumps(record, default=str),
            ),
        )


def get_channel_account(channel_account_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT data FROM channel_accounts WHERE channel_account_id = ?", (channel_account_id,)
        ).fetchone()
    return json.loads(row["data"]) if row else None


def list_channel_accounts() -> list[dict[str, Any]]:
    with _lock:
        conn = connect()
        rows = conn.execute("SELECT data FROM channel_accounts").fetchall()
    return [json.loads(r["data"]) for r in rows]


# --------------------------------------------------------------------------- conversation bindings


def get_binding(channel_account_id: str, external_conversation_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT data FROM conversation_bindings WHERE channel_account_id = ? AND external_conversation_id = ?",
            (channel_account_id, external_conversation_id),
        ).fetchone()
    return json.loads(row["data"]) if row else None


def insert_binding(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO conversation_bindings (binding_id, channel_account_id, external_conversation_id, "
        "conversation_id, session_id, agent_id, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record["binding_id"],
            record["channel_account_id"],
            record["external_conversation_id"],
            record["conversation_id"],
            record.get("session_id"),
            record.get("agent_id"),
            json.dumps(record, default=str),
        ),
    )


def get_binding_in(conn: sqlite3.Connection, channel_account_id: str, external_conversation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data FROM conversation_bindings WHERE channel_account_id = ? AND external_conversation_id = ?",
        (channel_account_id, external_conversation_id),
    ).fetchone()
    return json.loads(row["data"]) if row else None


# --------------------------------------------------------------------------- channel inbound dedupe


def get_inbound(channel_account_id: str, external_event_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT conversation_id, run_id, message_id FROM channel_inbound "
            "WHERE channel_account_id = ? AND external_event_id = ?",
            (channel_account_id, external_event_id),
        ).fetchone()
    return dict(row) if row else None


def record_inbound(
    channel_account_id: str, external_event_id: str, *,
    conversation_id: str, run_id: str | None, message_id: str, created_at: str,
) -> bool:
    """Record a processed inbound event. Returns False if it was already recorded (a retry)."""
    with transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM channel_inbound WHERE channel_account_id = ? AND external_event_id = ?",
            (channel_account_id, external_event_id),
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO channel_inbound (channel_account_id, external_event_id, conversation_id, "
            "run_id, message_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_account_id, external_event_id, conversation_id, run_id, message_id, created_at),
        )
    return True


# --------------------------------------------------------------------------- deliveries


def get_delivery(delivery_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute("SELECT data FROM deliveries WHERE delivery_id = ?", (delivery_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def insert_delivery(record: dict[str, Any]) -> bool:
    """Insert a delivery receipt. Returns False if the delivery_id was already recorded (idempotent)."""
    with transaction() as conn:
        exists = conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id = ?", (record["delivery_id"],)
        ).fetchone()
        if exists:
            return False
        conn.execute(
            "INSERT INTO deliveries (delivery_id, channel_account_id, run_id, status, created_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                record["delivery_id"],
                record.get("channel_account_id"),
                record.get("run_id"),
                record["status"],
                record.get("created_at"),
                json.dumps(record, default=str),
            ),
        )
    return True


# --------------------------------------------------------------------------- browser leases


def get_active_lease_for_profile(conn: sqlite3.Connection, profile_key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data FROM browser_leases WHERE profile_key = ? AND status = 'active'", (profile_key,)
    ).fetchone()
    return json.loads(row["data"]) if row else None


def insert_lease(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO browser_leases (binding_id, profile_key, identity_id, agent_id, run_id, status, "
        "acquired_at, heartbeat_at, expires_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record["binding_id"], record["profile_key"], record.get("identity_id"),
            record.get("agent_id"), record.get("run_id"), record["status"],
            record.get("acquired_at"), record.get("heartbeat_at"), record.get("expires_at"),
            json.dumps(record, default=str),
        ),
    )


def update_lease(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE browser_leases SET status=?, heartbeat_at=?, expires_at=?, data=? WHERE binding_id=?",
        (record["status"], record.get("heartbeat_at"), record.get("expires_at"),
         json.dumps(record, default=str), record["binding_id"]),
    )


def get_lease(binding_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect()
        row = conn.execute("SELECT data FROM browser_leases WHERE binding_id = ?", (binding_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def get_lease_in(conn: sqlite3.Connection, binding_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT data FROM browser_leases WHERE binding_id = ?", (binding_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def list_active_leases() -> list[dict[str, Any]]:
    with _lock:
        conn = connect()
        rows = conn.execute("SELECT data FROM browser_leases WHERE status = 'active'").fetchall()
    return [json.loads(r["data"]) for r in rows]
