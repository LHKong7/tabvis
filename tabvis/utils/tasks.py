"""File-backed task store for swarm coordination

A per-team (or per-session) on-disk task list under
``<tabvis-config-home>/tasks/<task-list-id>/``. Each task is a ``<id>.json`` file; a
``.highwatermark`` file records the maximum id ever assigned (so reset/delete don't reuse
ids), and a ``.lock`` sentinel file is the target of ``proper-lockfile`` directory locks that
serialize concurrent Tabvis processes in a swarm. The CRUD + claim/block/unassign helpers below
are the surface the Task* tools and the swarm runner consume.

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; the dict-shaped
``Task`` payloads round-trip to JSON on disk and to the SDK, so their keys stay verbatim wire
keys — camelCase ``activeForm`` / ``blockedBy`` (and ``status`` / ``blocks`` / ``owner`` /
``metadata`` / ``id`` / ``subject`` / ``description``). The pydantic ``TaskModel`` therefore
declares snake-case attrs with camelCase aliases + ``populate_by_name`` so both forms parse, and
``model_dump(by_alias=True)`` writes the wire form. ``ClaimTaskResult`` / ``AgentStatus`` /
``UnassignTasksResult`` are plain runtime dicts read by their wire keys (``busyWithTasks`` /
``blockedByTasks`` / ``currentTasks`` / ``unassignedTasks`` / ``notificationMessage``).

Zod → pydantic v2: ``TaskStatusSchema`` (an enum) and ``TaskSchema`` are kept behind
:func:`tabvis.utils.lazy_schema.lazy_schema` getters, exactly as the TS lazy-schema indirection,
so the schema is constructed once per session. ``TaskSchema`` validates with
``extra="forbid"`` (the Zod object is closed); unknown keys → validation failure → ``None`` from
:func:`get_task` (matching the TS ``safeParse`` failure path).

Locking: the TS ``proper-lockfile`` lock is the existing :mod:`tabvis.utils.lockfile` (stdlib
``mkdir``-based directory lock with the same retry/backoff and stale-takeover). The async TS
API maps to ``async``/``await`` here.

intra-repo imports resolve to the REAL existing modules
(:mod:`tabvis.bootstrap.state`, :mod:`tabvis.utils.array`, :mod:`tabvis.utils.debug`,
:mod:`tabvis.utils.env_utils`, :mod:`tabvis.utils.errors`, :mod:`tabvis.utils.lazy_schema`,
:mod:`tabvis.utils.lockfile`, :mod:`tabvis.utils.log`, :mod:`tabvis.utils.signal`,
:mod:`tabvis.utils.slow_operations`, :mod:`tabvis.utils.teammate`,
:mod:`tabvis.utils.teammate_context`).

The teams directory (``<tabvis-config-home>/teams``) is computed by a local ``_get_teams_dir``
helper, since :mod:`tabvis.utils.env_utils` exposes only ``get_tabvis_config_home_dir``.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from tabvis.bootstrap.state import get_is_non_interactive_session, get_session_id
from tabvis.utils import lockfile
from tabvis.utils.array import uniq
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy
from tabvis.utils.errors import get_errno_code, get_error_message
from tabvis.utils.lazy_schema import lazy_schema
from tabvis.utils.log import log_error
from tabvis.utils.signal import create_signal
from tabvis.utils.slow_operations import json_parse, json_stringify
from tabvis.utils.teammate import get_team_name
from tabvis.utils.teammate_context import get_teammate_context

# Listeners for task list updates (used for immediate UI refresh in same process)
_tasks_updated = create_signal()


# Team name set by the leader when creating a team. Used by get_task_list_id() so the leader's
# tasks are stored under the team name (matching where tmux/iTerm2 teammates look), not under
# the session ID.
_leader_team_name: str | None = None


def set_leader_team_name(team_name: str) -> None:
    """Set the leader's team name for task-list resolution (called by TeamCreate)."""
    global _leader_team_name
    if _leader_team_name == team_name:
        return
    _leader_team_name = team_name
    # Changing the task list ID is a "tasks updated" event for subscribers — they're now
    # looking at a different directory.
    notify_tasks_updated()


def clear_leader_team_name() -> None:
    """Clear the leader's team name (called when a team is deleted)."""
    global _leader_team_name
    if _leader_team_name is None:
        return
    _leader_team_name = None
    notify_tasks_updated()


# Register a listener to be called when tasks are updated in this process; returns an
# unsubscribe function. Bound to the signal's subscribe so it stays a stable reference.
on_tasks_updated = _tasks_updated.subscribe


def notify_tasks_updated() -> None:
    """Notify listeners that tasks have been updated.

    Wraps emit in try/except so listener failures never propagate to callers (task mutations
    must succeed from the caller's perspective).
    """
    try:
        _tasks_updated.emit()
    except Exception:  # noqa: BLE001 - listener errors must not fail task mutations
        pass


TASK_STATUSES = ("pending", "in_progress", "completed")

TaskStatus = Literal["pending", "in_progress", "completed"]

# Zod enum → pydantic. Kept behind lazy_schema (parity with the TS lazy-schema indirection).
TaskStatusSchema = lazy_schema(lambda: TaskStatus)


class TaskModel(BaseModel):
    """Pydantic model of a task.

    Wire keys are preserved verbatim (camelCase ``activeForm`` / ``blockedBy``); aliases +
    ``populate_by_name`` let both snake-case and wire forms parse. ``extra="forbid"`` mirrors the
    closed Zod object — unknown keys fail validation.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    subject: str
    description: str
    # Present-continuous form for the spinner (e.g. "Running tests").
    active_form: str | None = Field(default=None, alias="activeForm")
    # Agent ID.
    owner: str | None = None
    status: TaskStatus
    # Task IDs this task blocks.
    blocks: list[str]
    # Task IDs that block this task.
    blocked_by: list[str] = Field(alias="blockedBy")
    # Arbitrary metadata.
    metadata: dict[str, Any] | None = None


# A ``Task`` round-trips as a plain wire dict (camelCase keys). The CRUD helpers below accept and
# return these dicts so callers keep verbatim wire keys.
Task = dict[str, Any]

# Zod object → pydantic, behind lazy_schema (single stable schema per session).
TaskSchema = lazy_schema(lambda: TaskModel)


# High water mark file name — stores the maximum task ID ever assigned.
HIGH_WATER_MARK_FILE = ".highwatermark"

# Lock options: retry with backoff so concurrent callers (multiple Tabviss in a swarm) wait for
# the lock instead of failing immediately. Budget sized for ~10+ concurrent swarm agents.
LOCK_OPTIONS: lockfile.LockOptions = {
    "retries": {
        "retries": 30,
        "minTimeout": 5,
        "maxTimeout": 100,
    },
}


def _get_high_water_mark_path(task_list_id: str) -> str:
    return os.path.join(get_tasks_dir(task_list_id), HIGH_WATER_MARK_FILE)


async def _read_high_water_mark(task_list_id: str) -> int:
    path = _get_high_water_mark_path(task_list_id)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read().strip()
        try:
            return int(content, 10)
        except ValueError:
            return 0
    except OSError:
        return 0


async def _write_high_water_mark(task_list_id: str, value: int) -> None:
    path = _get_high_water_mark_path(task_list_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(value))


def is_todo_v2_enabled() -> bool:
    """Whether the Task* tools (todo v2) are active.

    Force-enabled in non-interactive mode (e.g. SDK users who want Task tools over TodoWrite)
    via ``TABVIS_ENABLE_TASKS``; otherwise enabled only for interactive sessions.
    """
    if is_env_truthy(os.environ.get("TABVIS_ENABLE_TASKS")):
        return True
    return not get_is_non_interactive_session()


async def reset_task_list(task_list_id: str) -> None:
    """Reset the task list for a new swarm — clears existing tasks.

    Writes a high water mark file to prevent ID reuse after reset, so task numbering starts at 1
    for the new swarm. Uses file locking to prevent races when multiple Tabviss run in parallel.
    """
    dir_path = get_tasks_dir(task_list_id)
    lock_path = await _ensure_task_list_lock_file(task_list_id)

    release = None
    try:
        # Acquire exclusive lock on the task list.
        release = await lockfile.lock(lock_path, LOCK_OPTIONS)

        # Find the current highest ID and save it to the high water mark file.
        current_highest = await _find_highest_task_id_from_files(task_list_id)
        if current_highest > 0:
            existing_mark = await _read_high_water_mark(task_list_id)
            if current_highest > existing_mark:
                await _write_high_water_mark(task_list_id, current_highest)

        # Delete all task files.
        try:
            files = os.listdir(dir_path)
        except OSError:
            files = []
        for file in files:
            if file.endswith(".json") and not file.startswith("."):
                file_path = os.path.join(dir_path, file)
                try:
                    os.unlink(file_path)
                except OSError:
                    # Ignore errors, file may already be deleted.
                    pass
        notify_tasks_updated()
    finally:
        if release:
            await release()


def get_task_list_id() -> str:
    """Resolve the task list ID from the current context.

    Priority:
    1. ``TABVIS_TASK_LIST_ID`` — explicit task list ID.
    2. In-process teammate: leader's team name (teammates share the leader's task list).
    3. ``TABVIS_TEAM_NAME`` / leader team name (via :func:`get_team_name`).
    4. Session ID — fallback for standalone sessions.
    """
    explicit = os.environ.get("TABVIS_TASK_LIST_ID")
    if explicit:
        return explicit
    # In-process teammates use the leader's team name so they share the same task list that
    # tmux/iTerm2 teammates also resolve to.
    teammate_ctx = get_teammate_context()
    if teammate_ctx:
        return teammate_ctx.team_name
    return get_team_name() or _leader_team_name or get_session_id()


_SANITIZE_PATH_COMPONENT_RE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_path_component(input_str: str) -> str:
    """Sanitize a string for safe use in file paths.

    Removes path-traversal and other dangerous characters — only alphanumerics, hyphens, and
    underscores survive (everything else becomes ``-``).
    """
    return _SANITIZE_PATH_COMPONENT_RE.sub("-", input_str)


def get_tasks_dir(task_list_id: str) -> str:
    return os.path.join(
        get_tabvis_config_home_dir(),
        "tasks",
        sanitize_path_component(task_list_id),
    )


def get_task_path(task_list_id: str, task_id: str) -> str:
    return os.path.join(
        get_tasks_dir(task_list_id), f"{sanitize_path_component(task_id)}.json"
    )


async def ensure_tasks_dir(task_list_id: str) -> None:
    dir_path = get_tasks_dir(task_list_id)
    try:
        os.makedirs(dir_path, exist_ok=True)
    except OSError:
        # Directory already exists or creation failed; callers will surface errors from
        # subsequent operations.
        pass


async def _find_highest_task_id_from_files(task_list_id: str) -> int:
    """Highest task ID from existing task files (not including the high water mark)."""
    dir_path = get_tasks_dir(task_list_id)
    try:
        files = os.listdir(dir_path)
    except OSError:
        return 0
    highest = 0
    for file in files:
        if not file.endswith(".json"):
            continue
        try:
            task_id = int(file.replace(".json", ""), 10)
        except ValueError:
            continue
        if task_id > highest:
            highest = task_id
    return highest


async def _find_highest_task_id(task_list_id: str) -> int:
    """Highest task ID ever assigned (existing files OR the high water mark)."""
    from_files = await _find_highest_task_id_from_files(task_list_id)
    from_mark = await _read_high_water_mark(task_list_id)
    return max(from_files, from_mark)


async def create_task(task_list_id: str, task_data: dict[str, Any]) -> str:
    """Create a new task with a unique ID (``task_data`` is a ``Task`` minus ``id``).

    Uses file locking to prevent races when multiple processes create tasks concurrently.
    Returns the new task's ID.
    """
    lock_path = await _ensure_task_list_lock_file(task_list_id)

    release = None
    try:
        release = await lockfile.lock(lock_path, LOCK_OPTIONS)

        # Read highest ID from disk while holding the lock.
        highest_id = await _find_highest_task_id(task_list_id)
        task_id = str(highest_id + 1)
        task: Task = {"id": task_id, **task_data}
        path = get_task_path(task_list_id, task_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(json_stringify(task, None, 2))
        notify_tasks_updated()
        return task_id
    finally:
        if release:
            await release()


async def get_task(task_list_id: str, task_id: str) -> Task | None:
    path = get_task_path(task_list_id, task_id)
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        data = json_parse(content)

        try:
            parsed = TaskSchema().model_validate(data)
        except Exception as exc:  # noqa: BLE001 - safeParse failure path
            log_for_debugging(
                f"[Tasks] Task {task_id} failed schema validation: {get_error_message(exc)}"
            )
            return None
        return parsed.model_dump(by_alias=True)
    except OSError as e:
        code = get_errno_code(e)
        if code == "ENOENT":
            return None
        log_for_debugging(f"[Tasks] Failed to read task {task_id}: {get_error_message(e)}")
        log_error(e)
        return None
    except Exception as e:  # noqa: BLE001 - parse/other failures mirror the TS catch
        log_for_debugging(f"[Tasks] Failed to read task {task_id}: {get_error_message(e)}")
        log_error(e)
        return None


async def _update_task_unsafe(
    task_list_id: str, task_id: str, updates: dict[str, Any]
) -> Task | None:
    """Internal: no lock. Callers already holding the taskPath lock must use this to avoid
    deadlock (claim_task, delete_task cascade, etc.)."""
    existing = await get_task(task_list_id, task_id)
    if not existing:
        return None
    updated: Task = {**existing, **updates, "id": task_id}
    path = get_task_path(task_list_id, task_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json_stringify(updated, None, 2))
    notify_tasks_updated()
    return updated


async def update_task(
    task_list_id: str, task_id: str, updates: dict[str, Any]
) -> Task | None:
    path = get_task_path(task_list_id, task_id)

    # Check existence before locking — proper-lockfile throws if the target file doesn't
    # exist, and we want a clean None result.
    task_before_lock = await get_task(task_list_id, task_id)
    if not task_before_lock:
        return None

    release = None
    try:
        release = await lockfile.lock(path, LOCK_OPTIONS)
        return await _update_task_unsafe(task_list_id, task_id, updates)
    finally:
        if release:
            await release()


async def delete_task(task_list_id: str, task_id: str) -> bool:
    path = get_task_path(task_list_id, task_id)

    try:
        # Update high water mark before deleting to prevent ID reuse.
        try:
            numeric_id = int(task_id, 10)
        except ValueError:
            numeric_id = None
        if numeric_id is not None:
            current_mark = await _read_high_water_mark(task_list_id)
            if numeric_id > current_mark:
                await _write_high_water_mark(task_list_id, numeric_id)

        # Delete the task file.
        try:
            os.unlink(path)
        except OSError as e:
            code = get_errno_code(e)
            if code == "ENOENT":
                return False
            raise

        # Remove references to this task from other tasks.
        all_tasks = await list_tasks(task_list_id)
        for task in all_tasks:
            new_blocks = [tid for tid in task["blocks"] if tid != task_id]
            new_blocked_by = [tid for tid in task["blockedBy"] if tid != task_id]
            if len(new_blocks) != len(task["blocks"]) or len(new_blocked_by) != len(
                task["blockedBy"]
            ):
                await update_task(
                    task_list_id,
                    task["id"],
                    {"blocks": new_blocks, "blockedBy": new_blocked_by},
                )

        notify_tasks_updated()
        return True
    except Exception:  # noqa: BLE001 - TS swallows all errors → False
        return False


async def list_tasks(task_list_id: str) -> list[Task]:
    dir_path = get_tasks_dir(task_list_id)
    try:
        files = os.listdir(dir_path)
    except OSError:
        return []
    task_ids = [f.replace(".json", "") for f in files if f.endswith(".json")]
    results = [await get_task(task_list_id, tid) for tid in task_ids]
    return [t for t in results if t is not None]


async def block_task(
    task_list_id: str, from_task_id: str, to_task_id: str
) -> bool:
    from_task = await get_task(task_list_id, from_task_id)
    to_task = await get_task(task_list_id, to_task_id)
    if not from_task or not to_task:
        return False

    # Update source task: A blocks B.
    if to_task_id not in from_task["blocks"]:
        await update_task(
            task_list_id,
            from_task_id,
            {"blocks": [*from_task["blocks"], to_task_id]},
        )

    # Update target task: B is blockedBy A.
    if from_task_id not in to_task["blockedBy"]:
        await update_task(
            task_list_id,
            to_task_id,
            {"blockedBy": [*to_task["blockedBy"], from_task_id]},
        )

    return True


ClaimTaskReason = Literal[
    "task_not_found",
    "already_claimed",
    "already_resolved",
    "blocked",
    "agent_busy",
]


class ClaimTaskResult(TypedDict, total=False):
    """Result of a claim attempt. Wire-shaped dict (camelCase ``busyWithTasks`` /
    ``blockedByTasks``)."""

    success: bool
    reason: ClaimTaskReason
    task: Task
    # Task IDs the agent is busy with (when reason is 'agent_busy').
    busyWithTasks: list[str]
    # Task IDs blocking this task (when reason is 'blocked').
    blockedByTasks: list[str]


def _get_task_list_lock_path(task_list_id: str) -> str:
    """Lock file path for a task list (used for list-level locking)."""
    return os.path.join(get_tasks_dir(task_list_id), ".lock")


async def _ensure_task_list_lock_file(task_list_id: str) -> str:
    """Ensure the lock file exists for a task list and return its path.

    proper-lockfile requires the target file to exist. Create it with the 'x' flag
    (write-exclusive) so concurrent callers don't both create it, and the first to create wins
    silently.
    """
    await ensure_tasks_dir(task_list_id)
    lock_path = _get_task_list_lock_path(task_list_id)
    try:
        with open(lock_path, "x", encoding="utf-8") as f:
            f.write("")
    except OSError:
        # EEXIST or other — file already exists, which is fine.
        pass
    return lock_path


class ClaimTaskOptions(TypedDict, total=False):
    """Options for :func:`claim_task`.

    ``check_agent_busy`` (wire key ``checkAgentBusy``): when true, atomically checks whether the
    agent already owns other open tasks before allowing the claim, using a task-list-level lock
    to prevent TOCTOU races.
    """

    checkAgentBusy: bool


async def claim_task(
    task_list_id: str,
    task_id: str,
    claimant_agent_id: str,
    options: ClaimTaskOptions | None = None,
) -> ClaimTaskResult:
    """Attempt to claim a task for an agent with file locking to prevent races.

    Returns ``success`` if the task was claimed, or a ``reason`` if not. When ``checkAgentBusy``
    is set, uses a task-list-level lock to atomically check whether the agent owns any other open
    tasks before claiming.
    """
    options = options or {}
    task_path = get_task_path(task_list_id, task_id)

    # Check existence before locking — proper-lockfile.lock throws if the target file doesn't
    # exist, and we want a clean task_not_found result.
    task_before_lock = await get_task(task_list_id, task_id)
    if not task_before_lock:
        return {"success": False, "reason": "task_not_found"}

    # If we need to check agent busy status, use task-list-level lock to prevent TOCTOU races.
    if options.get("checkAgentBusy"):
        return await _claim_task_with_busy_check(task_list_id, task_id, claimant_agent_id)

    # Otherwise, use task-level lock (original behavior).
    release = None
    try:
        release = await lockfile.lock(task_path, LOCK_OPTIONS)

        # Read current task state.
        task = await get_task(task_list_id, task_id)
        if not task:
            return {"success": False, "reason": "task_not_found"}

        # Check if already claimed by another agent.
        if task.get("owner") and task["owner"] != claimant_agent_id:
            return {"success": False, "reason": "already_claimed", "task": task}

        # Check if already resolved.
        if task["status"] == "completed":
            return {"success": False, "reason": "already_resolved", "task": task}

        # Check for unresolved blockers (open or in_progress tasks block).
        all_tasks = await list_tasks(task_list_id)
        unresolved_task_ids = {
            t["id"] for t in all_tasks if t["status"] != "completed"
        }
        blocked_by_tasks = [
            tid for tid in task["blockedBy"] if tid in unresolved_task_ids
        ]
        if len(blocked_by_tasks) > 0:
            return {
                "success": False,
                "reason": "blocked",
                "task": task,
                "blockedByTasks": blocked_by_tasks,
            }

        # Claim the task (already holding taskPath lock — use unsafe variant).
        updated = await _update_task_unsafe(
            task_list_id, task_id, {"owner": claimant_agent_id}
        )
        return {"success": True, "task": updated}  # type: ignore[typeddict-item]
    except Exception as error:  # noqa: BLE001 - TS catches and returns task_not_found
        log_for_debugging(
            f"[Tasks] Failed to claim task {task_id}: {get_error_message(error)}"
        )
        log_error(error)
        return {"success": False, "reason": "task_not_found"}
    finally:
        if release:
            await release()


async def _claim_task_with_busy_check(
    task_list_id: str, task_id: str, claimant_agent_id: str
) -> ClaimTaskResult:
    """Claim a task with an atomic check for agent-busy status.

    Uses a task-list-level lock to ensure the busy check and claim are atomic.
    """
    lock_path = await _ensure_task_list_lock_file(task_list_id)

    release = None
    try:
        release = await lockfile.lock(lock_path, LOCK_OPTIONS)

        # Read all tasks to check agent status and task state atomically.
        all_tasks = await list_tasks(task_list_id)

        # Find the task we want to claim.
        task = next((t for t in all_tasks if t["id"] == task_id), None)
        if not task:
            return {"success": False, "reason": "task_not_found"}

        # Check if already claimed by another agent.
        if task.get("owner") and task["owner"] != claimant_agent_id:
            return {"success": False, "reason": "already_claimed", "task": task}

        # Check if already resolved.
        if task["status"] == "completed":
            return {"success": False, "reason": "already_resolved", "task": task}

        # Check for unresolved blockers (open or in_progress tasks block).
        unresolved_task_ids = {
            t["id"] for t in all_tasks if t["status"] != "completed"
        }
        blocked_by_tasks = [
            tid for tid in task["blockedBy"] if tid in unresolved_task_ids
        ]
        if len(blocked_by_tasks) > 0:
            return {
                "success": False,
                "reason": "blocked",
                "task": task,
                "blockedByTasks": blocked_by_tasks,
            }

        # Check if agent is busy with other unresolved tasks.
        agent_open_tasks = [
            t
            for t in all_tasks
            if t["status"] != "completed"
            and t.get("owner") == claimant_agent_id
            and t["id"] != task_id
        ]
        if len(agent_open_tasks) > 0:
            return {
                "success": False,
                "reason": "agent_busy",
                "task": task,
                "busyWithTasks": [t["id"] for t in agent_open_tasks],
            }

        # Claim the task.
        updated = await update_task(task_list_id, task_id, {"owner": claimant_agent_id})
        return {"success": True, "task": updated}  # type: ignore[typeddict-item]
    except Exception as error:  # noqa: BLE001 - TS catches and returns task_not_found
        log_for_debugging(
            f"[Tasks] Failed to claim task {task_id} with busy check: "
            f"{get_error_message(error)}"
        )
        log_error(error)
        return {"success": False, "reason": "task_not_found"}
    finally:
        if release:
            await release()


class TeamMember(TypedDict, total=False):
    """Team member info (subset of TeamFile member structure). Wire-shaped (camelCase
    ``agentId`` / ``agentType``)."""

    agentId: str
    name: str
    agentType: str


class AgentStatus(TypedDict):
    """Agent status based on task ownership. Wire-shaped (camelCase ``agentId`` / ``agentType`` /
    ``currentTasks``)."""

    agentId: str
    name: str
    agentType: str | None
    status: Literal["idle", "busy"]
    # Task IDs the agent owns.
    currentTasks: list[str]


_SANITIZE_NAME_RE = re.compile(r"[^a-zA-Z0-9]")


def _sanitize_name(name: str) -> str:
    """Sanitize a name for use in file paths."""
    return _SANITIZE_NAME_RE.sub("-", name).lower()


# local reproduction (<tabvis-config-home>/teams). Switch to env_utils.get_teams_dir when implemented.
def _get_teams_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "teams")


async def _read_team_members(
    team_name: str,
) -> dict[str, Any] | None:
    """Read team members from the team file. Returns ``{leadAgentId, members}`` or ``None``."""
    teams_dir = _get_teams_dir()
    team_file_path = os.path.join(teams_dir, _sanitize_name(team_name), "config.json")
    try:
        with open(team_file_path, encoding="utf-8") as f:
            content = f.read()
        team_file = json_parse(content)
        return {
            "leadAgentId": team_file["leadAgentId"],
            "members": [
                {
                    "agentId": m["agentId"],
                    "name": m["name"],
                    "agentType": m.get("agentType"),
                }
                for m in team_file["members"]
            ],
        }
    except OSError as e:
        code = get_errno_code(e)
        if code == "ENOENT":
            return None
        log_for_debugging(
            f"[Tasks] Failed to read team file for {team_name}: {get_error_message(e)}"
        )
        return None
    except Exception as e:  # noqa: BLE001 - parse errors mirror the TS catch
        log_for_debugging(
            f"[Tasks] Failed to read team file for {team_name}: {get_error_message(e)}"
        )
        return None


async def get_agent_statuses(team_name: str) -> list[AgentStatus] | None:
    """Status of all agents in a team based on task ownership.

    An agent is "idle" if they own no open tasks, "busy" if they own at least one open task.
    Returns the agent statuses, or ``None`` if the team is not found.
    """
    team_data = await _read_team_members(team_name)
    if not team_data:
        return None

    task_list_id = _sanitize_name(team_name)
    all_tasks = await list_tasks(task_list_id)

    # Get unresolved tasks grouped by owner (open or in_progress).
    unresolved_tasks_by_owner: dict[str, list[str]] = {}
    for task in all_tasks:
        if task["status"] != "completed" and task.get("owner"):
            unresolved_tasks_by_owner.setdefault(task["owner"], []).append(task["id"])

    # Build status for each agent (leader is already in members).
    statuses: list[AgentStatus] = []
    for member in team_data["members"]:
        # Check both name (new) and agentId (legacy) for backwards compatibility.
        tasks_by_name = unresolved_tasks_by_owner.get(member["name"], [])
        tasks_by_id = unresolved_tasks_by_owner.get(member["agentId"], [])
        current_tasks = uniq([*tasks_by_name, *tasks_by_id])
        statuses.append(
            {
                "agentId": member["agentId"],
                "name": member["name"],
                "agentType": member.get("agentType"),
                "status": "idle" if len(current_tasks) == 0 else "busy",
                "currentTasks": current_tasks,
            }
        )
    return statuses


class UnassignTasksResult(TypedDict):
    """Result of unassigning tasks from a teammate. Wire-shaped (camelCase
    ``unassignedTasks`` / ``notificationMessage``)."""

    unassignedTasks: list[dict[str, str]]
    notificationMessage: str


async def unassign_teammate_tasks(
    team_name: str,
    teammate_id: str,
    teammate_name: str,
    reason: Literal["terminated", "shutdown"],
) -> UnassignTasksResult:
    """Unassign all open tasks from a teammate and build a notification message.

    Used when a teammate is killed or gracefully shuts down. Returns the unassigned tasks and a
    formatted notification message.
    """
    tasks = await list_tasks(team_name)
    unresolved_assigned_tasks = [
        t
        for t in tasks
        if t["status"] != "completed"
        and (t.get("owner") == teammate_id or t.get("owner") == teammate_name)
    ]

    # Unassign each task and reset status to pending.
    for task in unresolved_assigned_tasks:
        await update_task(
            team_name, task["id"], {"owner": None, "status": "pending"}
        )

    if len(unresolved_assigned_tasks) > 0:
        log_for_debugging(
            f"[Tasks] Unassigned {len(unresolved_assigned_tasks)} task(s) from {teammate_name}"
        )

    # Build notification message.
    action_verb = "was terminated" if reason == "terminated" else "has shut down"
    notification_message = f"{teammate_name} {action_verb}."
    if len(unresolved_assigned_tasks) > 0:
        task_list = ", ".join(
            f'#{t["id"]} "{t["subject"]}"' for t in unresolved_assigned_tasks
        )
        notification_message += (
            f" {len(unresolved_assigned_tasks)} task(s) were unassigned: {task_list}. "
            "Use TaskList to check availability and TaskUpdate with owner to reassign them to "
            "idle teammates."
        )

    return {
        "unassignedTasks": [
            {"id": t["id"], "subject": t["subject"]} for t in unresolved_assigned_tasks
        ],
        "notificationMessage": notification_message,
    }


DEFAULT_TASKS_MODE_TASK_LIST_ID = "tasklist"
