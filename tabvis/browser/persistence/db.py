"""SQLite ``runtime.db`` — the metadata store (PERS-2).

The relational half of the Persistence layer from ``design.md`` §"数据存储": a single ``runtime.db``
under the ``browser-os-data/`` root holding the agent / session / identity / workspace / artifact
metadata. It is introduced as a **best-effort shadow** — every write here is wrapped so a DB failure
is logged and swallowed, exactly like today's JSON writers, and JSON/JSONL remain the source of
truth (PERS-3 makes SQLite the *read* authority for the agent cold-load, with JSON as the fallback).

Each table keeps the queryable columns the design calls for (``browser_identities.agent_id`` is
``UNIQUE NOT NULL``, etc.) **plus** a ``data`` JSON blob, so a record round-trips losslessly while
still being indexable — no field is dropped when it is read back.

Concurrency: tabvis is single-process, but async writers reach here through ``asyncio.to_thread`` (real
worker threads), so the connection is opened ``check_same_thread=False`` and every access is
serialized under a re-entrant lock. WAL mode keeps readers from blocking the writer.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any, Callable

from tabvis.browser.persistence.paths import get_browser_os_data_dir, runtime_db_path
from tabvis.utils.debug import log_for_debugging

SCHEMA_VERSION = 1

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None

_DDL = (
    """CREATE TABLE IF NOT EXISTS agents (
        agent_id   TEXT PRIMARY KEY,
        session_id TEXT,
        status     TEXT,
        model      TEXT,
        profile    TEXT,
        created_at TEXT,
        ended_at   TEXT,
        data       TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        agent_id   TEXT,
        status     TEXT,
        engine     TEXT,
        updated_at TEXT,
        data       TEXT NOT NULL
    )""",
    # agent_id UNIQUE NOT NULL — the design's 1:1 agent↔identity constraint (design.md §1).
    """CREATE TABLE IF NOT EXISTS browser_identities (
        id         TEXT PRIMARY KEY,
        agent_id   TEXT NOT NULL UNIQUE,
        status     TEXT,
        created_at TEXT,
        updated_at TEXT,
        data       TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS workspaces (
        workspace_id TEXT PRIMARY KEY,
        agent_id     TEXT,
        identity_id  TEXT,
        profile      TEXT,
        session_id   TEXT,
        created_at   TEXT,
        data         TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS artifacts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        agent_id   TEXT,
        seq        INTEGER,
        type       TEXT,
        action     TEXT,
        url        TEXT,
        ts         TEXT,
        data       TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_workspaces_agent ON workspaces(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, seq)",
)


def is_sqlite_enabled() -> bool:
    """Whether the SQLite metadata store is active. ``TABVIS_BROWSER_SQLITE`` (default on).

    Off => every op below is a no-op and callers fall back to the JSON source of truth, so the DB can
    be disabled with zero behavioural change.
    """
    val = os.environ.get("TABVIS_BROWSER_SQLITE")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    for stmt in _DDL:
        conn.execute(stmt)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _connect() -> sqlite3.Connection | None:
    """Open (or reuse) the connection for the current config home. Reopens if the path changed."""
    global _conn, _conn_path
    path = runtime_db_path()
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:  # config home changed (e.g. a new test tmp dir) — reopen
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
    _migrate(conn)
    _conn, _conn_path = conn, path
    return _conn


def _run(op: Callable[[sqlite3.Connection], Any], default: Any = None) -> Any:
    """Run one DB op under the lock, best-effort. A failure is logged and swallowed."""
    if not is_sqlite_enabled():
        return default
    try:
        with _lock:
            conn = _connect()
            if conn is None:
                return default
            return op(conn)
    except Exception as e:  # noqa: BLE001 - the DB is a shadow; never fail a run over it
        log_for_debugging(f"[SQLITE] op failed: {e}")
        return default


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


# --------------------------------------------------------------------------- agents


def upsert_agent(record: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO agents (agent_id, session_id, status, model, profile, created_at, ended_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET session_id=excluded.session_id, status=excluded.status, "
            "model=excluded.model, profile=excluded.profile, created_at=excluded.created_at, "
            "ended_at=excluded.ended_at, data=excluded.data",
            (
                record.get("agent_id"),
                record.get("session_id"),
                record.get("status"),
                record.get("model"),
                record.get("profile"),
                record.get("created_at"),
                record.get("ended_at"),
                json.dumps(record, default=str),
            ),
        )
        conn.commit()

    _run(op)


def get_agent(agent_id: str) -> dict[str, Any] | None:
    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT data FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    return _run(op)


def list_agents() -> list[dict[str, Any]]:
    def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT data FROM agents").fetchall()
        return [json.loads(r["data"]) for r in rows]

    return _run(op, default=[]) or []


# --------------------------------------------------------------------------- sessions


def upsert_session(session_id: str, record: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        from tabvis.browser.session import utc_now

        agent = record.get("agent") or {}
        browser = record.get("browser") or {}
        conn.execute(
            "INSERT INTO sessions (session_id, agent_id, status, engine, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET agent_id=excluded.agent_id, status=excluded.status, "
            "engine=excluded.engine, updated_at=excluded.updated_at, data=excluded.data",
            (
                session_id,
                agent.get("agent_id"),  # AgentInfo carries no registry agent_id → NULL (not session_id)
                record.get("status"),
                (browser or {}).get("engine"),
                utc_now(),  # write-time stamp; the end time lives in the data blob's ended_at
                json.dumps(record, default=str),
            ),
        )
        conn.commit()

    _run(op)


def get_session(session_id: str) -> dict[str, Any] | None:
    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute("SELECT data FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    return _run(op)


# --------------------------------------------------------------------------- identities


def upsert_identity(record: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO browser_identities (id, agent_id, status, created_at, updated_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, "
            "data=excluded.data",
            (
                record.get("id"),
                record.get("agent_id"),
                record.get("status"),
                record.get("created_at"),
                record.get("updated_at"),
                json.dumps(record, default=str),
            ),
        )
        conn.commit()

    _run(op)


def get_identity_by_agent(agent_id: str) -> dict[str, Any] | None:
    def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT data FROM browser_identities WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        return json.loads(row["data"]) if row else None

    return _run(op)


def delete_identity(agent_id: str) -> None:
    """Remove an identity's row from the mirror (part of the delete-identity cascade)."""
    def op(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM browser_identities WHERE agent_id = ?", (agent_id,))
        conn.commit()

    _run(op)


# --------------------------------------------------------------------------- workspaces


def upsert_workspace(record: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO workspaces (workspace_id, agent_id, identity_id, profile, session_id, created_at, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workspace_id) DO UPDATE SET agent_id=excluded.agent_id, "
            "identity_id=excluded.identity_id, profile=excluded.profile, session_id=excluded.session_id, "
            "data=excluded.data",
            (
                record.get("workspace_id"),
                record.get("agent_id"),
                record.get("identity_id"),
                record.get("profile"),
                record.get("session_id"),
                record.get("created_at"),
                json.dumps(record, default=str),
            ),
        )
        conn.commit()

    _run(op)


def list_workspaces() -> list[dict[str, Any]]:
    def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT data FROM workspaces").fetchall()
        return [json.loads(r["data"]) for r in rows]

    return _run(op, default=[]) or []


# --------------------------------------------------------------------------- artifacts


def insert_artifact(session_id: str | None, agent_id: str | None, record: dict[str, Any]) -> None:
    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO artifacts (session_id, agent_id, seq, type, action, url, ts, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                agent_id or record.get("agent_id"),
                record.get("seq"),
                record.get("type"),
                record.get("action"),
                record.get("url"),
                record.get("ts"),
                json.dumps(record, default=str),
            ),
        )
        conn.commit()

    _run(op)


def list_artifacts(session_id: str) -> list[dict[str, Any]]:
    def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT data FROM artifacts WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    return _run(op, default=[]) or []
