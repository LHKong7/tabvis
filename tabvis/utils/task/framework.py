"""Task framework helpers

The shared state-machine helpers every task implementation builds on: AppState task updates,
registration (with SDK ``task_started`` emit), terminal eviction, the polling loop, and the
notification formatting. Tasks are plain ``dict`` records in the Tabvis runtime; ``set_app_state`` is
the React-style setter ``(updater) -> None`` where ``updater`` maps ``prev -> next``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from tabvis.constants.xml import (
    OUTPUT_FILE_TAG,
    STATUS_TAG,
    SUMMARY_TAG,
    TASK_ID_TAG,
    TASK_NOTIFICATION_TAG,
    TASK_TYPE_TAG,
    TOOL_USE_ID_TAG,
)
from tabvis.agent.task import is_terminal_task_status
from tabvis.utils.message_queue_manager import enqueue_pending_notification
from tabvis.utils.sdk_event_queue import enqueue_sdk_event
from tabvis.utils.task.disk_output import get_task_output_delta, get_task_output_path

# Standard polling interval for all tasks
POLL_INTERVAL_MS = 1000

# Duration to display killed tasks before eviction
STOPPED_DISPLAY_MS = 3_000

# Grace period for terminal local_agent tasks in the agent task panel
PANEL_GRACE_MS = 30_000

# AppState / task records are plain dicts in the Tabvis runtime.
AppState = dict[str, Any]
TaskState = dict[str, Any]
SetAppState = Callable[[Callable[[AppState], AppState]], None]


class TaskAttachment(TypedDict, total=False):
    """Attachment type for task status updates."""

    type: str
    taskId: str
    toolUseId: str | None
    taskType: str
    status: str
    description: str
    deltaSummary: str | None


def update_task_state(
    task_id: str,
    set_app_state: SetAppState,
    updater: Callable[[TaskState], TaskState],
) -> None:
    """Update a task's state in AppState.

    If the updater returns the same reference (early-return no-op), skips the spread so ``s.tasks``
    subscribers don't re-render on unchanged state.
    """

    def _apply(prev: AppState) -> AppState:
        tasks = prev.get("tasks") or {}
        task = tasks.get(task_id)
        if not task:
            return prev
        updated = updater(task)
        if updated is task:
            return prev
        return {
            **prev,
            "tasks": {
                **tasks,
                task_id: updated,
            },
        }

    set_app_state(_apply)


def register_task(task: TaskState, set_app_state: SetAppState) -> None:
    """Register a new task in AppState + emit ``task_started``."""
    is_replacement = {"value": False}

    def _apply(prev: AppState) -> AppState:
        tasks = prev.get("tasks") or {}
        existing = tasks.get(task["id"])
        is_replacement["value"] = existing is not None
        # Carry forward UI-held state on re-register (resumeAgentBackground replaces the task;
        # user's retain shouldn't reset).
        if existing and "retain" in existing:
            merged = {
                **task,
                "retain": existing.get("retain"),
                "startTime": existing.get("startTime"),
                "messages": existing.get("messages"),
                "diskLoaded": existing.get("diskLoaded"),
                "pendingMessages": existing.get("pendingMessages"),
            }
        else:
            merged = task
        return {**prev, "tasks": {**tasks, task["id"]: merged}}

    set_app_state(_apply)

    # Replacement (resume) — not a new start. Skip to avoid double-emit.
    if is_replacement["value"]:
        return

    enqueue_sdk_event(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": task["id"],
            "tool_use_id": task.get("toolUseId"),
            "description": task.get("description"),
            "task_type": task.get("type"),
            "workflow_name": task.get("workflowName") if "workflowName" in task else None,
            "prompt": task.get("prompt") if "prompt" in task else None,
        }
    )


def evict_terminal_task(task_id: str, set_app_state: SetAppState) -> None:
    """Eagerly evict a terminal+notified task from AppState."""

    def _apply(prev: AppState) -> AppState:
        tasks = prev.get("tasks") or {}
        task = tasks.get(task_id)
        if not task:
            return prev
        if not is_terminal_task_status(task.get("status")):
            return prev
        if not task.get("notified"):
            return prev
        # Panel grace period — blocks eviction until deadline passes.
        if "retain" in task and (task.get("evictAfter") or float("inf")) > _now_ms():
            return prev
        remaining_tasks = {k: v for k, v in tasks.items() if k != task_id}
        return {**prev, "tasks": remaining_tasks}

    set_app_state(_apply)


def get_running_tasks(state: AppState) -> list[TaskState]:
    """All tasks whose status is ``running``."""
    tasks = state.get("tasks") or {}
    return [task for task in tasks.values() if task.get("status") == "running"]


class _AttachmentResult(TypedDict):
    attachments: list[TaskAttachment]
    updatedTaskOffsets: dict[str, int]
    evictedTaskIds: list[str]


async def generate_task_attachments(state: AppState) -> _AttachmentResult:
    """Generate attachment messages for changed task states.

    Generate attachments for tasks with new output or status changes. Returns ONLY the offset
    patch (not full tasks) so a concurrent ``running -> completed`` transition during the async disk
    read isn't clobbered.
    """
    attachments: list[TaskAttachment] = []
    updated_task_offsets: dict[str, int] = {}
    evicted_task_ids: list[str] = []
    tasks = state.get("tasks") or {}

    for task_state in tasks.values():
        if task_state.get("notified"):
            status = task_state.get("status")
            if status in ("completed", "failed", "killed"):
                # Evict terminal tasks — they've been consumed and can be GC'd
                evicted_task_ids.append(task_state["id"])
                continue
            if status == "pending":
                # Keep in map — hasn't run yet, but parent already knows about it
                continue
            # running: fall through to running logic below

        if task_state.get("status") == "running":
            delta = await get_task_output_delta(
                task_state["id"],
                task_state.get("outputOffset") or 0,
            )
            if delta.get("content"):
                updated_task_offsets[task_state["id"]] = delta["newOffset"]

        # Completed tasks are NOT notified here — each task type handles its own
        # completion notification via enqueue_pending_notification().

    return {
        "attachments": attachments,
        "updatedTaskOffsets": updated_task_offsets,
        "evictedTaskIds": evicted_task_ids,
    }


def apply_task_offsets_and_evictions(
    set_app_state: SetAppState,
    updated_task_offsets: dict[str, int],
    evicted_task_ids: list[str],
) -> None:
    """Apply the task offsets and evictions.

    Merges patches against FRESH ``prev.tasks`` (not the stale pre-await snapshot), so concurrent
    status transitions aren't clobbered.
    """
    offset_ids = list(updated_task_offsets.keys())
    if len(offset_ids) == 0 and len(evicted_task_ids) == 0:
        return

    def _apply(prev: AppState) -> AppState:
        changed = False
        new_tasks = {**(prev.get("tasks") or {})}
        for task_id in offset_ids:
            fresh = new_tasks.get(task_id)
            # Re-check status on fresh state — task may have completed during the await.
            if fresh and fresh.get("status") == "running":
                new_tasks[task_id] = {**fresh, "outputOffset": updated_task_offsets[task_id]}
                changed = True
        for task_id in evicted_task_ids:
            fresh = new_tasks.get(task_id)
            # Re-check terminal+notified on fresh state (TOCTOU: resume may have replaced the task).
            if (
                not fresh
                or not is_terminal_task_status(fresh.get("status"))
                or not fresh.get("notified")
            ):
                continue
            if "retain" in fresh and (fresh.get("evictAfter") or float("inf")) > _now_ms():
                continue
            del new_tasks[task_id]
            changed = True
        return {**prev, "tasks": new_tasks} if changed else prev

    set_app_state(_apply)


async def poll_tasks(
    get_app_state: Callable[[], AppState],
    set_app_state: SetAppState,
) -> None:
    """Poll all running tasks and check for updates."""
    state = get_app_state()
    result = await generate_task_attachments(state)
    attachments = result["attachments"]
    updated_task_offsets = result["updatedTaskOffsets"]
    evicted_task_ids = result["evictedTaskIds"]

    apply_task_offsets_and_evictions(set_app_state, updated_task_offsets, evicted_task_ids)

    # Send notifications for completed tasks
    for attachment in attachments:
        _enqueue_task_notification(attachment)


def _enqueue_task_notification(attachment: TaskAttachment) -> None:
    """Format + enqueue a task notification."""
    status_text = _get_status_text(attachment["status"])

    output_path = get_task_output_path(attachment["taskId"])
    tool_use_id = attachment.get("toolUseId")
    tool_use_id_line = (
        f"\n<{TOOL_USE_ID_TAG}>{tool_use_id}</{TOOL_USE_ID_TAG}>" if tool_use_id else ""
    )
    message = (
        f"<{TASK_NOTIFICATION_TAG}>\n"
        f"<{TASK_ID_TAG}>{attachment['taskId']}</{TASK_ID_TAG}>{tool_use_id_line}\n"
        f"<{TASK_TYPE_TAG}>{attachment['taskType']}</{TASK_TYPE_TAG}>\n"
        f"<{OUTPUT_FILE_TAG}>{output_path}</{OUTPUT_FILE_TAG}>\n"
        f"<{STATUS_TAG}>{attachment['status']}</{STATUS_TAG}>\n"
        f'<{SUMMARY_TAG}>Task "{attachment["description"]}" {status_text}</{SUMMARY_TAG}>\n'
        f"</{TASK_NOTIFICATION_TAG}>"
    )

    enqueue_pending_notification({"value": message, "mode": "task-notification"})


def _get_status_text(status: str) -> str:
    """Human-readable status text."""
    return {
        "completed": "completed successfully",
        "failed": "failed",
        "killed": "was stopped",
        "running": "is running",
        "pending": "is pending",
    }.get(status, "")


def _now_ms() -> int:
    """``Date.now()`` — current epoch in milliseconds."""
    import time

    return int(time.time() * 1000)
