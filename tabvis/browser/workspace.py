"""Workspace — a first-class, ``workspace_id``-addressable Workspace object (WS-1).

``design.md`` §2 makes Workspace a one-class object carrying Agent ID / Goal / Task / Pages /
Artifacts / Timeline, distinct from Identity. Today the code conflates the two: ``manager._Workspace``
is simultaneously the persistent Chromium, the identity (its profile dir), and the "workspace". This
module introduces the Workspace *record* additively — a ``workspace_id`` minted alongside
``init_browser_session`` and a read-only :func:`snapshot` view — without changing how tools drive the
browser. It references its identity by ``identity_ref`` (today the profile dir; WS-2 formalizes the
split), and derives Pages/Artifacts from data that already exists (the session record's tabs+history,
the artifacts store). Goal/Task/Timeline are stubs here (Task is seeded from the agent's run prompt);
promoting them to first-class fields is WS-3/WS-4/WS-5.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from tabvis.browser.session import utc_now
from tabvis.utils.debug import log_for_debugging


def new_workspace_id() -> str:
    return f"ws_{uuid.uuid4().hex[:16]}"


@dataclass
class WorkspaceRecord:
    """One workspace — what an agent is currently doing, addressable by ``workspace_id``.

    Identity is referenced by indirection, not embedded: ``identity_id`` names the agent's
    :class:`~tabvis.browser.identity.BrowserIdentity` (WS-2), decoupling the workspace from the
    raw profile. ``identity_ref`` remains the Chromium ``user_data_dir`` for back-compat / the no-op
    split — the two coincide today because identity == profile dir.
    """

    workspace_id: str
    agent_id: str
    identity_ref: str                 # the profile user_data_dir (== where the profile lives)
    identity_id: str | None = None    # WS-2: the BrowserIdentity this workspace inherits
    profile: str | None = None
    session_id: str | None = None
    goal: str | None = None           # WS-5
    status: str = "active"            # WS-6: active | paused | closed
    created_at: str = field(default_factory=utc_now)


# Process-local registries (rebuilt per process; WS-7 re-attaches them from SQLite on demand).
_by_id: dict[str, WorkspaceRecord] = {}
_id_by_agent: dict[str, str] = {}
_persisted_loaded = False


def _ensure_loaded() -> None:
    """WS-7: re-attach persisted workspaces (from the SQLite mirror) once, lazily.

    This is what lets an agent's workspace — with its goal/task/pages linkage — survive a process
    restart and be picked up again on reuse. Best-effort and additive: a missing/disabled DB simply
    yields nothing.
    """
    global _persisted_loaded
    if _persisted_loaded:
        return
    _persisted_loaded = True
    try:
        from tabvis.browser.persistence import db

        for data in db.list_workspaces():
            workspace_id = data.get("workspace_id")
            if not workspace_id or workspace_id in _by_id:
                continue
            record = WorkspaceRecord(
                workspace_id=workspace_id,
                agent_id=data.get("agent_id", ""),
                identity_ref=data.get("identity_ref", ""),
                identity_id=data.get("identity_id"),
                profile=data.get("profile"),
                session_id=data.get("session_id"),
                goal=data.get("goal"),
                status=data.get("status", "active"),
                created_at=data.get("created_at") or utc_now(),
            )
            _by_id[workspace_id] = record
            _id_by_agent.setdefault(record.agent_id, workspace_id)
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[WORKSPACE] sqlite re-attach skipped: {e}")


def register_workspace(
    *,
    agent_id: str,
    user_data_dir: str,
    profile: str | None = None,
    session_id: str | None = None,
    identity_id: str | None = None,
) -> WorkspaceRecord:
    """Mint (or return the existing) workspace for an agent. Idempotent per agent.

    An agent bundles one browser for its life (``manager``), so it has one workspace: a second call
    for the same ``agent_id`` returns the same record rather than minting a new id. ``identity_id``
    (WS-2) links the workspace to the agent's resolved identity; it backfills an existing record that
    was minted before the identity was resolved.
    """
    _ensure_loaded()  # WS-7: re-attach a persisted workspace for this agent before minting a new one
    existing_id = _id_by_agent.get(agent_id)
    if existing_id is not None and existing_id in _by_id:
        record = _by_id[existing_id]
        # Keep the record's session / identity current across re-runs of the same agent, and mark it
        # live again (it may have been persisted/loaded as 'paused' or 'closed').
        if session_id:
            record.session_id = session_id
        if identity_id and not record.identity_id:
            record.identity_id = identity_id
        record.status = "active"
        _mirror(record)
        return record

    record = WorkspaceRecord(
        workspace_id=new_workspace_id(),
        agent_id=agent_id,
        identity_ref=user_data_dir,
        identity_id=identity_id,
        profile=profile,
        session_id=session_id,
    )
    _by_id[record.workspace_id] = record
    _id_by_agent[agent_id] = record.workspace_id
    _mirror(record)
    return record


def _mirror(record: WorkspaceRecord) -> None:
    """PERS-2: mirror the workspace into the SQLite metadata store (best-effort, additive)."""
    try:
        from dataclasses import asdict

        from tabvis.browser.persistence import db

        db.upsert_workspace(asdict(record))
    except Exception:  # noqa: BLE001 - the in-memory record is authoritative
        pass


def get_workspace(workspace_id: str) -> WorkspaceRecord | None:
    _ensure_loaded()
    return _by_id.get(workspace_id)


def get_workspace_for_agent(agent_id: str) -> WorkspaceRecord | None:
    _ensure_loaded()
    wid = _id_by_agent.get(agent_id)
    return _by_id.get(wid) if wid else None


def list_workspace_records() -> list[WorkspaceRecord]:
    _ensure_loaded()
    return list(_by_id.values())


def _page_id(url: str | None, index: int) -> str:
    """A stable-ish page id: same URL → same id across snapshots; positional fallback otherwise."""
    if url:
        return "pg_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"pg_{index}"


def _pages_for(agent_id: str) -> list[dict[str, Any]]:
    """First-class Page entries (WS-3), derived from the agent's live browser-session tabs.

    Each carries a stable ``page_id`` (keyed by URL) so an artifact or a timeline entry can reference
    a page. Read straight off the existing session record, so it has real content without a new store.
    """
    try:
        from tabvis.browser.manager import get_session_record

        record = get_session_record(agent_id)
        if record is None:
            return []
        pages: list[dict[str, Any]] = []
        for index, raw in enumerate(record.tabs):
            tab = raw if isinstance(raw, dict) else {}
            url = tab.get("url")
            pages.append(
                {
                    "page_id": _page_id(url, index),
                    "url": url,
                    "title": tab.get("title"),
                    "active": bool(tab.get("active")),
                }
            )
        return pages
    except Exception:  # noqa: BLE001 - snapshot is a read-only view; never raise
        return []


def _timeline_for(agent_id: str) -> list[dict[str, Any]]:
    """The agent's semantic-observation timeline (WS-5), fed by the observation pipeline (OBS-4)."""
    try:
        from tabvis.browser.observation import get_timeline

        return get_timeline(agent_id)
    except Exception:  # noqa: BLE001
        return []


def _task_for(agent_id: str) -> str | None:
    """The agent's current task — today the run prompt (WS-5 makes Task/Goal first-class)."""
    try:
        from tabvis.agent.agents import registry

        record = registry.get(agent_id)
        return record.prompt if record is not None else None
    except Exception:  # noqa: BLE001
        return None


def _artifacts_for(session_id: str | None) -> dict[str, Any]:
    try:
        from tabvis.browser.artifacts import artifacts_summary

        return artifacts_summary(session_id)
    except Exception:  # noqa: BLE001
        return {"count": 0, "by_type": {}, "last_url": None}


def snapshot(workspace_id: str) -> dict[str, Any] | None:
    """A JSON-safe Workspace Snapshot (``design.md``: return a view, not internal objects).

    Carries the six design fields: Agent ID, Goal (stub), Task (the run prompt), Pages (tabs +
    history), Artifacts (summary), Timeline (stub). None if the id is unknown.
    """
    _ensure_loaded()  # WS-7: resolve persisted workspaces (parity with the other read accessors)
    record = _by_id.get(workspace_id)
    if record is None:
        return None
    return {
        "workspace_id": record.workspace_id,
        "agent_id": record.agent_id,
        "identity_id": record.identity_id,        # WS-2: the resolved BrowserIdentity
        "identity_ref": record.identity_ref,
        "profile": record.profile,
        "session_id": record.session_id,
        "status": record.status,                   # WS-6: active | paused | closed
        "goal": record.goal,                       # WS-5
        "task": _task_for(record.agent_id),
        "pages": _pages_for(record.agent_id),      # WS-3: first-class page entries
        "artifacts": _artifacts_for(record.session_id),
        "timeline": _timeline_for(record.agent_id),  # WS-5: semantic-observation timeline (OBS-4)
        "created_at": record.created_at,
    }


# --------------------------------------------------------------------------- WS-5 / WS-6: mutations + manager


def set_goal(workspace_id: str, goal: str | None) -> WorkspaceRecord | None:
    """Set the workspace's Goal (WS-5). None if the id is unknown."""
    record = _by_id.get(workspace_id)
    if record is None:
        return None
    record.goal = goal
    _mirror(record)
    return record


def set_status(workspace_id: str, status: str) -> WorkspaceRecord | None:
    record = _by_id.get(workspace_id)
    if record is None:
        return None
    record.status = status
    _mirror(record)
    return record


def pause(workspace_id: str) -> WorkspaceRecord | None:
    """Pause a workspace (WS-6). The browser stays open; the workspace is marked paused."""
    return set_status(workspace_id, "paused")


def resume(workspace_id: str) -> WorkspaceRecord | None:
    return set_status(workspace_id, "active")


async def close_workspace(workspace_id: str) -> bool:
    """Close a workspace (WS-6): close its browser and free the profile. True if one was closed."""
    record = _by_id.get(workspace_id)
    if record is None:
        return False
    record.status = "closed"
    _mirror(record)
    try:
        from tabvis.browser.manager import close_browser

        return await close_browser(record.identity_ref)
    except Exception:  # noqa: BLE001
        return False


def list_workspace_snapshots() -> list[dict[str, Any]]:
    """A snapshot view of every registered workspace (WS-6, for GET /workspaces)."""
    _ensure_loaded()
    return [snap for wid in list(_by_id) if (snap := snapshot(wid)) is not None]


class WorkspaceManager:
    """Facade over the workspace functions (WS-6): create / open / pause / close / snapshot.

    Not a separate process — a thin object over the in-process registry, matching ``design.md``'s
    Workspace Manager responsibilities (create / open / pause / close and return a Snapshot rather
    than internals).
    """

    def create(
        self,
        *,
        agent_id: str,
        user_data_dir: str,
        profile: str | None = None,
        session_id: str | None = None,
        identity_id: str | None = None,
    ) -> WorkspaceRecord:
        return register_workspace(
            agent_id=agent_id,
            user_data_dir=user_data_dir,
            profile=profile,
            session_id=session_id,
            identity_id=identity_id,
        )

    def open(self, workspace_id: str) -> dict[str, Any] | None:
        """Return the workspace's live snapshot (open is implicit — the browser launches lazily)."""
        return snapshot(workspace_id)

    def pause(self, workspace_id: str) -> WorkspaceRecord | None:
        return pause(workspace_id)

    def resume(self, workspace_id: str) -> WorkspaceRecord | None:
        return resume(workspace_id)

    async def close(self, workspace_id: str) -> bool:
        return await close_workspace(workspace_id)

    def snapshot(self, workspace_id: str) -> dict[str, Any] | None:
        return snapshot(workspace_id)

    def list(self) -> list[dict[str, Any]]:
        return list_workspace_snapshots()


_manager: WorkspaceManager | None = None


def get_workspace_manager() -> WorkspaceManager:
    """The process-wide :class:`WorkspaceManager`."""
    global _manager
    if _manager is None:
        _manager = WorkspaceManager()
    return _manager
