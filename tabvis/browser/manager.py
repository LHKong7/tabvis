"""Browser manager — a browser *workspace* **bundled to the agent that owns it**.

An agent and its browser are one unit. When the agent spawns it **reserves** a workspace (the
``owner_agent``) and — unless eager launch is opted out — launches the Chromium there and then. That
browser is the agent's environment for its whole life: it is NOT released at the end of a run and it
is NOT reaped for being idle. The window, its tabs, its scroll position and its logins stay exactly
as the agent left them, run after run, until the **user quits the agent** (or the process exits).

    agent (spawns)  ──owns──▶  one persistent browser  ──lives until──▶  the user quits the agent

Ownership vs. driving — two distinct facts about a workspace:
* ``owner_agent`` — the agent this workspace is **bundled to**. Set once at spawn
  (:func:`init_browser_session`), held for the agent's whole life, cleared only by an explicit
  :func:`close_browser` / quit. A second agent may not bundle a workspace another agent owns.
* ``busy_agent``  — the owner **while a run is actively driving the page**. Set when driving,
  cleared by :func:`detach_agent` at the end of a run. It exists so a mid-action browser is never
  closed out from under a live run, and so the concurrency guard can refuse two interleaving runs.

Lifecycle
---------
* ``init_browser_session`` bundles the workspace to the agent (reserves ``owner_agent``) at spawn,
  and refuses if another agent already owns it. Eager launch follows via ``start_browser_warmup``.
* ``get_or_create_browser_service`` launches on first use (if not already warm) or hands back the
  live one — always the owner's.
* ``detach_agent`` (end of a run) releases ``busy_agent`` only. The bundle and the **browser stay**.
* ``close_browser`` / quit is the ONLY thing that closes a bundled browser and frees its profile —
  that is the "user quit them" step. Process shutdown closes everything via the cleanup registry.

Profile ↔ agent is **1:1**: an agent owns exactly one profile and a profile belongs to exactly one
agent. The **profile name is the agent's stable identity** — the same name always maps to the same
directory (:func:`resolve_profile_dir`), which is how a persistent agent re-attaches to its browser
(tabs + logins) across runs; a profile is not a pool shared between agents over time. Both directions
are enforced: :func:`init_browser_session` refuses to rebind an agent to a second profile, and
``owner_agent`` refuses a second agent on a profile another already owns. This also satisfies
Chromium's single-writer lock on a profile directory (concurrent agents must use different profiles).

Which agent a tool call belongs to rides on a :class:`~contextvars.ContextVar`, so every tool call
inside an agent's task resolves to the right browser without threading ``agent_id`` everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from tabvis.browser.browser_service import BrowserService
from tabvis.browser.session import (
    AgentInfo,
    BrowserSessionRecord,
    utc_now,
    write_browser_session,
)
from tabvis.utils.browser_config import (
    get_browser_engine,
    get_browser_idle_timeout_ms,
    get_browser_user_data_dir,
    is_browser_eager_disabled,
    playwright_available,
    scrub_secrets,
)
from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.debug import log_for_debugging

DEFAULT_AGENT_ID = "default"
DEFAULT_PROFILE = "default"

_current_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tabvis_current_agent_id", default=DEFAULT_AGENT_ID
)


@dataclass
class _Workspace:
    """One persistent browser, keyed by its profile dir. Outlives any individual agent run."""

    user_data_dir: str
    profile: str
    service: BrowserService | None = None
    launch_task: asyncio.Task[BrowserService] | None = None
    # The agent this workspace is BUNDLED to. Set at spawn, held for the agent's whole life, cleared
    # only by an explicit close/quit. While set, the workspace is never idle-reaped and no other
    # agent may bundle it.
    owner_agent: str | None = None
    # The owner *while a run is actively driving the page*. Released at the end of each run; the
    # bundle (owner_agent) outlives it. Guards against closing a mid-action browser and against two
    # interleaving runs.
    busy_agent: str | None = None
    last_used_at: float = 0.0


@dataclass
class _Slot:
    """What one agent knows about its workspace (its own record; the browser is shared)."""

    agent_id: str
    profile: str
    user_data_dir: str
    record: BrowserSessionRecord | None = None
    persist: bool = True


_workspaces: dict[str, _Workspace] = {}   # user_data_dir -> the persistent browser
_slots: dict[str, _Slot] = {}             # agent_id      -> that agent's view
_cleanup_registered = False
_reaper: asyncio.Task[None] | None = None


# --------------------------------------------------------------------------- agent binding


def bind_agent(agent_id: str) -> contextvars.Token[str]:
    return _current_agent.set(agent_id)


def unbind_agent(token: contextvars.Token[str]) -> None:
    with contextlib.suppress(ValueError):
        _current_agent.reset(token)


def current_agent_id() -> str:
    return _current_agent.get()


_PROFILE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _profile_slug(identity: str) -> str:
    """A filesystem-safe, collision-resistant slug for a profile identity.

    The identity comes from user input (a ``profile`` name off an API payload), so it must not be
    able to escape the profiles dir (``..``, slashes) or collapse to the reserved ``default``. Unsafe
    runs become ``-``, leading/trailing dots+dashes are stripped, and anything that would slug to
    empty (``..``, ``///``) falls back to a hash of the original so distinct names never collide.
    """
    slug = _PROFILE_SLUG_RE.sub("-", identity.strip()).strip(".-")
    if not slug:
        slug = "p-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return slug[:128]


def resolve_profile_dir(agent_id: str, profile: str | None) -> str:
    """Map an agent's profile identity → its ONE workspace directory (profile ↔ agent is 1:1).

    A profile is bound to exactly one agent and an agent to exactly one profile. The **profile name
    is the agent's stable identity**: the same name always resolves to the same directory, which is
    what lets a persistent agent re-attach to its browser (tabs + logins) across runs. A profile is
    NOT a pool that different agents share over time — ``owner_agent`` holds it for the owner's life.

    - ``"default"`` → the base logged-in workspace (the primary/CLI agent's browser).
    - any other name → ``<base>/profiles/<slug>``, keyed by the name.
    - no name given → the agent's own id is used as the identity (an ephemeral per-agent profile),
      so the binding is still 1:1 even when the caller does not name one.
    """
    base = get_browser_user_data_dir()
    identity = (profile or agent_id or "").strip()
    if identity == DEFAULT_PROFILE:
        return base
    return os.path.join(base, "profiles", _profile_slug(identity))


def get_profile_holder(user_data_dir: str, *, exclude: str | None = None) -> str | None:
    """The agent *currently driving* this workspace (mid-run), if any — else None.

    This is about **active driving**, not ownership: between runs an owned-but-idle workspace
    returns None here, which is what lets a quit/close proceed when nothing is mid-action. For "who
    owns this bundle", use :func:`get_workspace_owner`.
    """
    ws = _workspaces.get(user_data_dir)
    if ws is None or ws.busy_agent is None or ws.busy_agent == exclude:
        return None
    return ws.busy_agent


def get_workspace_owner(user_data_dir: str, *, exclude: str | None = None) -> str | None:
    """The agent this workspace is **bundled to**, if any — held for that agent's whole life.

    Unlike :func:`get_profile_holder` this does not go away between runs: an agent owns its browser
    from spawn until it is quit, so a second agent asking for the same profile is refused the whole
    time. This is the check the run dispatcher uses before it lets a new agent take a profile.
    """
    ws = _workspaces.get(user_data_dir)
    if ws is None or ws.owner_agent is None or ws.owner_agent == exclude:
        return None
    return ws.owner_agent


def list_workspaces() -> list[dict[str, Any]]:
    """Every live browser, for introspection (`GET /browsers`)."""
    out = []
    for ws in _workspaces.values():
        alive = ws.service is not None and ws.service.is_alive()
        if not alive:
            continue
        out.append(
            {
                "profile": ws.profile,
                "user_data_dir": ws.user_data_dir,
                "owner_agent": ws.owner_agent,   # the agent this browser is bundled to
                "busy_agent": ws.busy_agent,     # the owner, only while a run is driving
                "bundled": ws.owner_agent is not None,
                "idle_seconds": round(time.monotonic() - ws.last_used_at, 1)
                if ws.busy_agent is None and ws.last_used_at
                else 0,
                "tabs": ws.service.tabs() if ws.service else [],
                "browser": ws.service.browser_info() if ws.service else {},
            }
        )
    return out


# --------------------------------------------------------------------------- session record


def init_browser_session(
    *,
    session_id: str,
    model: str,
    cwd: str,
    agent_id: str = DEFAULT_AGENT_ID,
    profile: str | None = DEFAULT_PROFILE,
    persist: bool = True,
) -> BrowserSessionRecord:
    """Bundle a workspace to an agent at spawn and reserve it for the agent's whole life.

    This is the "spawn the agent's browser" step. It **reserves** the workspace to ``agent_id``
    (``owner_agent``) so it is the agent's own for life — held across runs, never idle-reaped, and
    off-limits to any other agent — and refuses if another agent already owns it. The actual launch
    is eager (``start_browser_warmup``) or lazy (first tool call); either way ownership is fixed here.

    Raises ``RuntimeError`` if the target workspace is already bundled to a *different* agent (the
    run dispatcher should have refused first via :func:`get_workspace_owner`; this is the backstop).
    """
    user_data_dir = resolve_profile_dir(agent_id, profile)

    # 1:1, the agent→profile direction: an agent binds ONE profile for its whole life. A re-run for
    # the same agent must name the same profile (→ same dir, allowed to re-attach); asking to bind a
    # *different* profile is a rebind and is refused. (owner_agent below enforces the reverse.)
    existing = _slots.get(agent_id)
    if existing is not None and existing.user_data_dir != user_data_dir:
        raise RuntimeError(
            f"agent {agent_id!r} is already bound to profile {existing.profile!r}; an agent binds "
            f"exactly one profile for its life. Quit it before rebinding to {profile or agent_id!r}."
        )

    ws = _workspaces.get(user_data_dir)
    if ws is None:
        ws = _Workspace(user_data_dir=user_data_dir, profile=profile or agent_id)
        _workspaces[user_data_dir] = ws
    if ws.owner_agent is not None and ws.owner_agent != agent_id:
        # Bundled to someone else, and an agent holds its browser for life — so this is a hard
        # conflict, not a wait-your-turn. Point at the fix rather than silently sharing a page.
        raise RuntimeError(
            f"browser profile {ws.profile!r} is bundled to agent {ws.owner_agent!r} for its whole "
            f"life (an agent owns its browser until it is quit). Use a different profile, omit it "
            f"for an isolated one, or quit that agent first."
        )

    slot = _Slot(
        agent_id=agent_id,
        profile=profile or agent_id,
        user_data_dir=user_data_dir,
        persist=persist,
    )
    slot.record = BrowserSessionRecord(
        agent=AgentInfo(session_id=session_id, model=model, cwd=cwd, pid=os.getpid())
    )
    _slots[agent_id] = slot

    # Claim the bundle NOW, at spawn. Both owner (held for life) and busy (this run is about to
    # drive) are the agent itself — 1:1 ownership means reserving here can never steal a live page,
    # the very race the old lazy-claim guarded against, because no other agent may own this dir.
    ws.owner_agent = agent_id
    ws.busy_agent = agent_id
    ws.last_used_at = time.monotonic()

    # IDP-3: materialize the agent's durable identity at spawn (resolve-or-create), recording the
    # profile dir as its profile source. Best-effort — the identity is metadata over the real bundle.
    identity_id: str | None = None
    try:
        from tabvis.browser import identity_store

        identity_id = identity_store.resolve(agent_id, profile_ref=user_data_dir).id
    except Exception:  # noqa: BLE001 - additive seam; never break spawn
        pass

    # WS-1 / WS-2: mint (or re-attach) a first-class workspace_id for this agent, linked to the
    # identity by id (the identity_ref indirection). Best-effort — the browser bundle is what matters.
    try:
        from tabvis.browser import workspace as _workspace

        _workspace.register_workspace(
            agent_id=agent_id,
            user_data_dir=user_data_dir,
            profile=slot.profile,
            session_id=session_id,
            identity_id=identity_id,
        )
    except Exception:  # noqa: BLE001 - additive seam; never break spawn
        pass

    # IDP-4: acquire an IdentityBinding for this run (flips the identity to in_use). Best-effort —
    # the real exclusivity is still owner_agent/busy_agent above; the binding is metadata over it.
    try:
        from tabvis.browser import identity_store
        from tabvis.browser import workspace as _ws

        ws_record = _ws.get_workspace_for_agent(agent_id)
        identity_store.acquire(agent_id, ws_record.workspace_id if ws_record else None)
    except Exception:  # noqa: BLE001 - additive seam; never break spawn
        pass

    _adopt_running_browser(slot)      # stamp the record from an already-open browser (same owner)
    return slot.record


def _adopt_running_browser(slot: _Slot) -> None:
    """Stamp the record from an already-open workspace — this is the 'inherit' path."""
    ws = _workspaces.get(slot.user_data_dir)
    if slot.record is None or ws is None or ws.service is None or not ws.service.is_alive():
        return
    slot.record.status = "ready"
    slot.record.browser = ws.service.browser_info()
    slot.record.tabs = ws.service.tabs()


def _slot(agent_id: str | None = None) -> _Slot | None:
    return _slots.get(agent_id or current_agent_id())


async def _persist(slot: _Slot) -> None:
    if slot.persist and slot.record is not None:
        await write_browser_session(slot.record)


def get_session_record(agent_id: str | None = None) -> BrowserSessionRecord | None:
    slot = _slot(agent_id)
    return slot.record if slot else None


def get_session_summary(agent_id: str | None = None) -> dict[str, Any]:
    record = get_session_record(agent_id)
    return record.summary() if record is not None else {}


async def record_activity(url: str | None = None, title: str | None = None) -> None:
    slot = _slot()
    if slot is None or slot.record is None:
        return
    ws = _workspaces.get(slot.user_data_dir)
    if ws is not None:
        ws.last_used_at = time.monotonic()
        if ws.service is not None and ws.service.is_alive():
            slot.record.tabs = ws.service.tabs()
    if url:
        last = slot.record.history[-1]["url"] if slot.record.history else None
        if url != last:
            slot.record.add_navigation(url, title or "")
    await _persist(slot)


# --------------------------------------------------------------------------- warm-up / access


def start_browser_warmup() -> asyncio.Task[BrowserService] | None:
    if not playwright_available() or is_browser_eager_disabled():
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    task = asyncio.ensure_future(_warm_up())
    task.add_done_callback(_swallow_task_error)
    return task


async def _warm_up() -> BrowserService:
    service = await get_or_create_browser_service()
    slot = _slot()
    if slot is not None and slot.record is not None:
        _adopt_running_browser(slot)
        await _persist(slot)
    return service


def _swallow_task_error(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # A cloak launch failure carries the proxy URL in its message — scrub before logging.
        log_for_debugging(
            f"[BROWSER] warm-up failed (will retry lazily): {scrub_secrets(str(exc))}"
        )


def get_browser_service(agent_id: str | None = None) -> BrowserService | None:
    """The live browser for this agent's workspace, or None. Sync, non-launching."""
    slot = _slot(agent_id)
    if slot is None:
        return None
    ws = _workspaces.get(slot.user_data_dir)
    return ws.service if ws else None


async def get_or_create_browser_service() -> BrowserService:
    """The agent's workspace browser — launching it, or **inheriting the live one**."""
    agent_id = current_agent_id()
    slot = _slots.get(agent_id)
    if slot is None:
        # A tool ran without stream_agent binding a slot (library/test use) — default workspace.
        user_data_dir = resolve_profile_dir(agent_id, DEFAULT_PROFILE)
        slot = _Slot(agent_id=agent_id, profile=DEFAULT_PROFILE, user_data_dir=user_data_dir)
        _slots[agent_id] = slot

    ws = _workspaces.get(slot.user_data_dir)
    if ws is None:
        ws = _Workspace(user_data_dir=slot.user_data_dir, profile=slot.profile)
        _workspaces[slot.user_data_dir] = ws

    # Re-affirm the wheel for this run. Ownership was normally reserved at spawn
    # (init_browser_session), so busy_agent is already this agent; this branch also covers the
    # eager-launch task and a library caller that reached a tool with no spawn (it bundles to
    # itself). Refuse a workspace bundled to — or being driven by — a *different* agent. The
    # check-and-set is atomic (single-threaded loop, no await between), so two agents can never end
    # up driving one page.
    other = ws.owner_agent if ws.owner_agent not in (None, agent_id) else ws.busy_agent
    if other is not None and other != agent_id:
        raise RuntimeError(
            f"browser workspace {ws.profile!r} is bundled to agent {other!r} (Chromium locks a "
            f"profile to one process). Use a different profile, or quit that agent first."
        )
    ws.owner_agent = ws.owner_agent or agent_id
    ws.busy_agent = agent_id
    ws.last_used_at = time.monotonic()

    if ws.service is not None and ws.service.is_alive():
        wanted = get_browser_engine()
        running = ws.service.browser_info().get("engine")
        if running == wanted:
            return ws.service                              # <- inherit the persistent browser
        # Engine flipped under a workspace that is still open. By default each engine has its own
        # profile dir, so this only happens when TABVIS_BROWSER_USER_DATA_DIR pins both to one
        # directory — but inheriting anyway would silently hand a run that asked for stealth a
        # stock-Chromium browser, and it would look like it worked. Close and relaunch instead.
        log_for_debugging(
            f"[BROWSER] workspace {ws.profile!r} is running the {running!r} engine but this run "
            f"wants {wanted!r}; closing it and relaunching."
        )
        # Close it INLINE rather than via close_browser(), which would release ws.busy_agent across
        # an await and let a concurrent agent claim the workspace mid-swap. We keep the wheel.
        stale, ws.service, ws.launch_task = ws.service, None, None
        with contextlib.suppress(BaseException):
            await stale.close()

    if ws.launch_task is None or ws.launch_task.done():
        ws.launch_task = asyncio.ensure_future(_launch_new(ws))
    return await ws.launch_task


async def _launch_new(ws: _Workspace) -> BrowserService:
    service = BrowserService()
    slot = _slot()
    try:
        await service.launch(user_data_dir=ws.user_data_dir)
    except BaseException as e:
        if slot is not None and slot.record is not None:
            slot.record.status = "failed"
            # A cloak launch failure echoes the full Chromium argv, which carries the proxy URL
            # (user:pass) verbatim. record.error is persisted to browser-session.json and served
            # over the unauthenticated API, so it must be scrubbed — the same posture browser_info()
            # already takes for the structured proxy field.
            slot.record.error = scrub_secrets(f"{type(e).__name__}: {e}")
            await _persist(slot)
        raise

    ws.service = service
    ws.last_used_at = time.monotonic()
    _ensure_cleanup_registered()
    _ensure_reaper_running()
    if slot is not None and slot.record is not None:
        slot.record.status = "ready"
        slot.record.browser = service.browser_info()
        slot.record.tabs = service.tabs()
        await _persist(slot)
    # RT-5: open a Session Registry lease for this browser (lease/heartbeat). Best-effort.
    try:
        import time as _time

        from tabvis.browser import session_registry

        if slot is not None and slot.record is not None:
            session_registry.acquire(
                slot.record.agent.session_id, slot.agent_id, now_ts=_time.time()
            )
    except Exception:  # noqa: BLE001 - lease tracking is additive
        pass
    return service


# --------------------------------------------------------------------------- detach / teardown


async def detach_agent(agent_id: str | None = None) -> None:
    """End an agent's RUN but keep its **bundled** browser open and still owned by the agent.

    The run finishes, so the agent stops *actively driving* (``busy_agent`` is released) — but the
    bundle is not touched: ``owner_agent`` stays, the browser stays up, and the window keeps its
    tabs and logins for the agent's next run. Only a quit/close (or process exit) ends the bundle.
    """
    slot = _slots.get(agent_id or current_agent_id())
    if slot is None:
        return
    ws = _workspaces.get(slot.user_data_dir)
    if ws is not None and ws.busy_agent == slot.agent_id:
        ws.busy_agent = None              # no longer driving; the bundle (owner_agent) is untouched
        ws.last_used_at = time.monotonic()
    if slot.record is not None and ws is not None and ws.service is not None and ws.service.is_alive():
        slot.record.tabs = ws.service.tabs()
        slot.record.status = "ready"      # the browser is still up — not "closed"
        await _persist(slot)


async def close_browser(user_data_dir: str) -> bool:
    """End a bundle: close the browser (if any) and free the profile. The "user quit them" step.

    Returns True if a live browser was actually closed, False if there was nothing running. Either
    way the **bundle is released** — a workspace reserved at spawn but never launched (a non-browsing
    agent, or lazy launch that never fired) still has its ownership cleared here, so the profile
    becomes usable by a new agent. This is the ONE place a bundle actually ends (bar process exit).
    """
    ws = _workspaces.get(user_data_dir)
    if ws is None:
        return False
    service = ws.service
    try:
        if service is not None:
            await service.close()
    except BaseException as e:  # noqa: BLE001 - anyio/Cancelled/ExceptionGroup teardown noise
        log_for_debugging(f"[BROWSER] close failed for {ws.profile!r}: {e}")
    finally:
        # Clear ownership so the profile is free for a new agent and the reaper/guards see it as
        # unowned — whether or not a browser was ever launched behind this reservation.
        ws.service = None
        ws.launch_task = None
        ws.busy_agent = None
        ws.owner_agent = None
        # Snapshot _slots: _persist() awaits (asyncio.to_thread), and a concurrent init_browser_session
        # can insert into _slots during that suspension — iterating the live dict would then raise
        # "dictionary changed size during iteration" (matches the list(_workspaces) pattern elsewhere).
        for slot in list(_slots.values()):
            if slot.user_data_dir == user_data_dir and slot.record is not None:
                slot.record.status = "closed"
                slot.record.ended_at = utc_now()
                await _persist(slot)
        # IDP-4: release each agent's IdentityBinding for this workspace (flips its identity → ready).
        try:
            from tabvis.browser import identity_store

            for slot in list(_slots.values()):
                if slot.user_data_dir == user_data_dir:
                    identity_store.release_for_agent(slot.agent_id)
        except Exception:  # noqa: BLE001 - best-effort
            pass
        # RT-5: release the session lease(s) for this workspace (a clean close, not a crash).
        try:
            from tabvis.browser import session_registry

            for slot in list(_slots.values()):
                if slot.user_data_dir == user_data_dir and slot.record is not None:
                    session_registry.release(slot.record.agent.session_id)
        except Exception:  # noqa: BLE001 - best-effort
            pass
    return service is not None


async def shutdown_browser_service(agent_id: str | None = None) -> None:
    """Close the browser this agent is using (explicit teardown, not the normal end-of-run path)."""
    slot = _slots.get(agent_id or current_agent_id())
    if slot is None:
        return
    await close_browser(slot.user_data_dir)


async def quit_agent_browser(agent_id: str) -> bool:
    """End an agent's bundle: close its browser and free the profile. The "user quit them" step.

    Returns False if the agent had no workspace / nothing was open. Safe to call on an agent that
    never launched a browser (a non-browsing run) — it simply reports False.
    """
    slot = _slots.get(agent_id)
    if slot is None:
        return False
    return await close_browser(slot.user_data_dir)


async def shutdown_all_browsers() -> None:
    """Close every workspace. Registered with the cleanup registry; runs at process exit."""
    dirs = list(_workspaces)
    if not dirs:
        return
    await asyncio.gather(*(close_browser(d) for d in dirs), return_exceptions=True)


def _ensure_cleanup_registered() -> None:
    global _cleanup_registered
    if not _cleanup_registered:
        _cleanup_registered = True
        register_cleanup(shutdown_all_browsers)


# --------------------------------------------------------------------------- idle reaper


def _ensure_reaper_running() -> None:
    """Reap only **unowned** idle workspaces — a bundled browser lives until its agent is quit.

    A workspace bundled to an agent (``owner_agent`` set) is that agent's environment for its whole
    life and is deliberately never idle-reaped. The reaper only exists to catch a genuinely orphaned
    workspace — one with no owner, e.g. a library/test caller that never quit it.
    """
    global _reaper
    if _reaper is not None and not _reaper.done():
        return
    if get_browser_idle_timeout_ms() <= 0:
        return  # 0 => never reap; the workspace lives until shutdown
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    _reaper = asyncio.ensure_future(_reap_idle_loop())
    _reaper.add_done_callback(_swallow_task_error)


def _is_reapable(ws: _Workspace, now: float, timeout_ms: int) -> bool:
    """Whether the idle reaper may close this workspace right now.

    A workspace is reapable only when it is open, not actively driven, **not bundled to an agent**,
    and has sat idle past the timeout. The ``owner_agent`` guard is the whole point of the bundled
    model: an agent's browser is never idle-reaped — it lives until the agent is quit.
    """
    if ws.service is None or ws.busy_agent is not None:
        return False           # not open, or actively being driven
    if ws.owner_agent is not None:
        return False           # bundled to an agent — never idle-reaped
    return (now - ws.last_used_at) * 1000 >= timeout_ms


async def _reap_idle_loop() -> None:
    while True:
        timeout_ms = get_browser_idle_timeout_ms()
        if timeout_ms <= 0:
            return
        await asyncio.sleep(min(60.0, timeout_ms / 1000 / 4))
        now = time.monotonic()
        for user_data_dir, ws in list(_workspaces.items()):
            if _is_reapable(ws, now, timeout_ms):
                log_for_debugging(
                    f"[BROWSER] closing idle workspace {ws.profile!r} "
                    f"(idle {int(now - ws.last_used_at)}s)"
                )
                await close_browser(user_data_dir)
