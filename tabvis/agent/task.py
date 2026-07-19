"""Shared state model for the background tasks implemented by Tabvis.

Shell commands use this module to generate task IDs. Workflows additionally use
:func:`create_task_state_base` to register their state in ``AppState["tasks"]``. Task output is
stored in the per-session directory managed by :mod:`tabvis.utils.task.disk_output`.

Stored dictionaries retain the API/SDK wire keys such as ``toolUseId``, ``startTime`` and
``outputFile``.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from tabvis.utils.task.disk_output import get_task_output_path

TaskType = Literal["local_bash", "local_workflow"]
TaskStatus = Literal["pending", "running", "completed", "failed", "killed"]

TASK_ID_PREFIXES: dict[str, str] = {
    "local_bash": "b",
    "local_workflow": "w",
}
TASK_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


SetAppState = Callable[[Callable[[Any], Any]], None]


class TaskStateBase(TypedDict, total=False):
    """Fields shared by registered background workflow tasks."""

    id: str
    type: TaskType
    status: TaskStatus
    description: str
    toolUseId: str
    startTime: int
    outputFile: str
    outputOffset: int
    notified: bool


def is_terminal_task_status(status: str | None) -> bool:
    """Return whether a background task has reached a final state."""
    return status in ("completed", "failed", "killed")


def _get_task_id_prefix(task_type: str) -> str:
    return TASK_ID_PREFIXES.get(task_type, "x")


def generate_task_id(task_type: str) -> str:
    """Generate a compact, unpredictable ID prefixed by task type."""
    prefix = _get_task_id_prefix(task_type)
    raw = secrets.token_bytes(8)
    chars = [TASK_ID_ALPHABET[byte % len(TASK_ID_ALPHABET)] for byte in raw]
    return prefix + "".join(chars)


def create_task_state_base(
    task_id: str,
    task_type: TaskType,
    description: str,
    tool_use_id: str | None = None,
) -> TaskStateBase:
    """Build the state registered before a workflow begins running."""
    return {
        "id": task_id,
        "type": task_type,
        "status": "pending",
        "description": description,
        "toolUseId": tool_use_id,
        "startTime": _now_ms(),
        "outputFile": get_task_output_path(task_id),
        "outputOffset": 0,
        "notified": False,
    }


def _now_ms() -> int:
    """Current epoch time in milliseconds."""
    return int(time.time() * 1000)
