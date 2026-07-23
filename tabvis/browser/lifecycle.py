"""Agent lifecycle operations — deliberately distinct and isolated (Resume Plus §14.1, §6.2).

The design's core lifecycle rule: quitting a browser, suspending an agent, clearing a profile,
deleting artifacts, forgetting memory, and deleting an agent are **separate operations**, and none
may silently do another's job (§14.1). This module is that boundary — each function composes the
existing primitives (the browser manager, ``data_clearing``, the memory store) to do exactly one
thing, and its return value states what it kept vs. removed.

The guarantees the acceptance criteria name:

* ``quit_browser`` / ``suspend_agent`` close the browser process but keep the profile, the memory, and
  the raw evidence — they never retire the agent or delete anything durable.
* ``clear_profile`` deletes only the Chromium profile (after closing it), bumps the profile
  generation so the reset is intentional, and keeps memory + evidence.
* ``forget_memory`` touches only Agent Memory — it never closes the browser or logs the user out.
* ``delete_agent`` is the ONLY composite, and it deletes profile / memory / artifacts only when the
  caller explicitly asks.

``browser_status`` exposes resident-browser count vs. capacity so usage is bounded and observable.
"""

from __future__ import annotations

import os
import shutil
from typing import Any

from tabvis.browser import profile_generation
from tabvis.utils.debug import log_for_debugging

_DEFAULT_MAX_AGENTS = 4


def _capacity() -> int:
    try:
        return max(1, int(os.environ.get("TABVIS_SERVER_MAX_AGENTS") or _DEFAULT_MAX_AGENTS))
    except ValueError:
        return _DEFAULT_MAX_AGENTS


# --------------------------------------------------------------------------- browser process


async def quit_browser(agent_id: str) -> dict[str, Any]:
    """Close the agent's browser process and free its profile bundle. Keeps EVERYTHING durable.

    The "user quit them" action (§14.1): the durable agent, its persistent profile directory, its
    Agent Memory, and its raw evidence are all untouched — a later Resume enters recovery and
    relaunches the same profile.
    """
    from tabvis.browser.manager import quit_agent_browser

    closed = await quit_agent_browser(agent_id)
    return {"agent_id": agent_id, "browser_closed": closed,
            "profile_kept": True, "memory_kept": True, "evidence_kept": True}


async def suspend_agent(agent_id: str) -> dict[str, Any]:
    """Close the browser process while keeping the durable binding + memory (§6.2 `suspend`).

    Distinct from :func:`quit_browser` only in intent — suspension expects the agent to be resumed.
    Nothing durable is released.
    """
    from tabvis.browser.manager import quit_agent_browser

    closed = await quit_agent_browser(agent_id)
    return {"agent_id": agent_id, "suspended": True, "browser_closed": closed,
            "profile_kept": True, "memory_kept": True}


# --------------------------------------------------------------------------- profile


async def clear_profile(
    agent_id: str, *, profile: str | None = None, wait: bool = False,
) -> dict[str, Any]:
    """Delete ONLY the agent's Chromium profile, after closing it. Keeps memory + evidence (§14.1).

    Closes the browser first (a live profile cannot be cleared), bumps the profile generation so the
    next Resume reports an intentional reset rather than an unexpected missing profile (§6.2), then
    hands the directory to the guarded :func:`tabvis.browser.data_clearing.clear_profile` (managed-root
    check, atomic move to trash, async purge, audit). Agent Memory is NOT touched.
    """
    from tabvis.browser.data_clearing import clear_profile as _fs_clear_profile
    from tabvis.browser.manager import quit_agent_browser, resolve_profile_dir

    await quit_agent_browser(agent_id)  # a profile in use cannot be cleared
    user_data_dir = resolve_profile_dir(agent_id, profile)
    gen = profile_generation.bump(agent_id, reason="clear_profile")
    result = _fs_clear_profile(user_data_dir, agent_id=agent_id, reason="clear_profile", wait=wait)
    return {"agent_id": agent_id, "profile_cleared": result.get("cleared", False),
            "profile_generation": gen.generation, "memory_kept": True, "evidence_kept": True,
            "detail": result}


# --------------------------------------------------------------------------- evidence


def delete_artifacts(session_id: str) -> dict[str, Any]:
    """Delete a session's raw browser artifacts (events + DOM blobs). Keeps Agent Memory (§14.1).

    Committed Memory summaries survive; only the raw evidence is removed (its provenance may later be
    marked unavailable). Downloads in the workspace are a separate cleanup and are left alone.
    """
    from tabvis.browser.artifacts import get_artifacts_dir

    directory = get_artifacts_dir(session_id)
    removed = os.path.isdir(directory)
    if removed:
        shutil.rmtree(directory, ignore_errors=True)
    return {"session_id": session_id, "artifacts_removed": removed, "memory_kept": True}


# --------------------------------------------------------------------------- memory (thin passthrough)


def forget_memory(
    principal_id: str, agent_id: str, target_type: str, target_id: str, *, reason: str = "",
) -> dict[str, Any]:
    """Forget one Agent Memory item. Touches ONLY memory — never the browser/profile (§14.1).

    This is the "forget does not log the user out" guarantee: cookies/logins live in the persistent
    profile, which this never touches.
    """
    from tabvis.agent.mem.agent_store import AgentMemoryStore

    store = AgentMemoryStore.open_for(principal_id, agent_id)
    ts = store.forget(target_type, target_id, reason=reason)  # type: ignore[arg-type]
    return {"agent_id": agent_id, "forgotten": target_id, "tombstone_seq": ts.seq,
            "browser_untouched": True, "profile_untouched": True}


# --------------------------------------------------------------------------- composite delete


async def delete_agent(
    principal_id: str,
    agent_id: str,
    *,
    delete_profile: bool = False,
    delete_memory: bool = False,
    delete_artifacts_for: list[str] | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """The ONLY composite: retire an agent, deleting durable data only where explicitly requested.

    Always closes the browser process. ``delete_profile`` clears the Chromium profile;
    ``delete_memory`` removes the Agent Memory namespace AND its identity secrets;
    ``delete_artifacts_for`` removes the named sessions' raw evidence. Nothing durable is removed
    without its flag — so this can never *accidentally* delete a profile or memory (§14.1 acceptance).
    """
    from tabvis.browser.manager import quit_agent_browser

    summary: dict[str, Any] = {"agent_id": agent_id, "browser_closed": False,
                               "profile_deleted": False, "memory_deleted": False,
                               "artifacts_deleted": []}
    summary["browser_closed"] = await quit_agent_browser(agent_id)

    if delete_profile:
        try:
            res = await clear_profile(agent_id, profile=profile, wait=True)
            summary["profile_deleted"] = bool(res.get("profile_cleared"))
        except Exception as e:  # noqa: BLE001 - report, don't abort the composite
            summary["profile_error"] = str(e)

    if delete_memory:
        try:
            from tabvis.agent.mem.agent_store import AgentMemoryStore

            AgentMemoryStore.open_for(principal_id, agent_id).delete_all()
            summary["memory_deleted"] = True
        except Exception as e:  # noqa: BLE001
            summary["memory_error"] = str(e)
        try:
            from tabvis.browser import identity_store

            identity_store.delete_identity(agent_id)  # cascades secret deletion
            summary["identity_deleted"] = True
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[LIFECYCLE] identity delete failed for {agent_id}: {e}")

    for session_id in (delete_artifacts_for or []):
        try:
            delete_artifacts(session_id)
            summary["artifacts_deleted"].append(session_id)
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[LIFECYCLE] artifact delete failed for {session_id}: {e}")

    return summary


# --------------------------------------------------------------------------- observability


def browser_status() -> dict[str, Any]:
    """Resident-browser capacity + a snapshot of owned workspaces (§6.2/§18: bounded + observable)."""
    from tabvis.browser.manager import list_workspaces

    workspaces = list_workspaces()
    resident = sum(1 for w in workspaces if w.get("bundled"))
    capacity = _capacity()
    return {
        "resident": resident,
        "capacity": capacity,
        "at_capacity": resident >= capacity,
        "workspaces": workspaces,
    }
