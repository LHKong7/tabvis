"""Browser session record — links the agent session to the browser it drives, on disk.

Every headless run that warms up a browser writes a ``browser-session.json`` into that run's
session directory, next to the existing ``tool-results/`` and ``subagents/`` sidecars::

    <config-home>/projects/<sanitized-cwd>/<session-id>/browser-session.json

The record captures both halves of the pairing:

* **agent**   — session id, model, cwd, process pid, start time.
* **browser** — persistent-profile dir, headless/channel/viewport/executable, launch time, the
  Playwright driver pid, plus the live tab list and the navigation history.

It is rewritten (atomically, tmp + ``os.replace``) whenever the browser reaches a new lifecycle
state and after every navigation, and finalized with ``ended_at``/``status`` at shutdown — so a
completed run leaves a full audit trail of where the agent browsed.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from tabvis.utils.debug import log_for_debugging

BROWSER_SESSION_FILENAME = "browser-session.json"

# Bound the navigation history so a long-running scrape can't grow the record without limit.
MAX_HISTORY_ENTRIES = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class AgentInfo:
    """Who is driving — the agent side of the pairing."""

    session_id: str
    model: str
    cwd: str
    pid: int
    started_at: str = field(default_factory=utc_now)


@dataclass
class BrowserSessionRecord:
    """The agent↔browser pairing for one run."""

    agent: AgentInfo
    # launching -> ready -> closed  (or -> failed)
    status: str = "launching"
    browser: dict[str, Any] | None = None
    tabs: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
    ended_at: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": asdict(self.agent),
            "browser": self.browser,
            "status": self.status,
            "tabs": self.tabs,
            "history": self.history,
            "ended_at": self.ended_at,
            "error": self.error,
        }

    def summary(self) -> dict[str, Any]:
        """A compact, JSON-safe view for app_state (no live Playwright objects, ever)."""
        return {
            "sessionId": self.agent.session_id,
            "model": self.agent.model,
            "status": self.status,
            "engine": (self.browser or {}).get("engine"),
            "stealth": (self.browser or {}).get("stealth"),
            "profileDir": (self.browser or {}).get("profile_dir"),
            "headless": (self.browser or {}).get("headless"),
            "currentUrl": self.history[-1]["url"] if self.history else None,
            "tabCount": len(self.tabs),
            "navigations": len(self.history),
            "recordPath": get_browser_session_path(),
        }

    def add_navigation(self, url: str, title: str) -> None:
        self.history.append({"url": url, "title": title, "at": utc_now()})
        if len(self.history) > MAX_HISTORY_ENTRIES:
            del self.history[: len(self.history) - MAX_HISTORY_ENTRIES]


def get_session_dir() -> str:
    """``<config-home>/projects/<sanitized-cwd>/<session-id>/`` — created lazily by writers."""
    from tabvis.bootstrap.state import get_original_cwd, get_session_id
    from tabvis.utils.session_storage_portable import get_project_dir

    return os.path.join(get_project_dir(get_original_cwd()), str(get_session_id()))


def get_browser_session_path() -> str:
    return os.path.join(get_session_dir(), BROWSER_SESSION_FILENAME)


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)  # atomic: readers never see a half-written record


async def write_browser_session(record: BrowserSessionRecord) -> None:
    """Persist the record. Best-effort — a failed write must never break the agent run."""
    try:
        await asyncio.to_thread(_write_json_atomic, get_browser_session_path(), record.to_dict())
    except Exception as e:  # noqa: BLE001 - persistence is best-effort
        log_for_debugging(f"[BROWSER] failed to write browser-session.json: {e}")
    # PERS-2: mirror the session into the SQLite metadata store (best-effort; JSON stays authoritative).
    try:
        from tabvis.browser.persistence import db

        await asyncio.to_thread(db.upsert_session, record.agent.session_id, record.to_dict())
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[BROWSER] failed to mirror session to sqlite: {e}")


def read_browser_session(session_dir: str | None = None) -> dict[str, Any] | None:
    """Read back a persisted record (for inspection/tests). None if absent or unreadable."""
    path = (
        os.path.join(session_dir, BROWSER_SESSION_FILENAME)
        if session_dir
        else get_browser_session_path()
    )
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None
