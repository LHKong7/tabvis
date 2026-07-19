"""SDK event queue

A bounded process-global FIFO of out-of-band ``system`` SDK events (task lifecycle +
session-state transitions) that headless/streaming consumers drain straight into the output
stream. Events are wire envelopes round-tripping to SDK output, so their keys stay verbatim
snake_case (``task_id``, ``tool_use_id``, ``output_file``, ``session_id`` …) — only Python
identifiers change.

Faithful-behavior notes:
- The queue is a module-level ``list`` capped at :data:`MAX_QUEUE_SIZE` (1000). On overflow the
  oldest entry is dropped (``queue.shift()`` → ``pop(0)``).
- :func:`enqueue_sdk_event` is a no-op unless the session is non-interactive — in TUI mode events
  would accumulate to the cap and never be read. Gated on
  :func:`tabvis.bootstrap.state.get_is_non_interactive_session`.
- :func:`drain_sdk_events` splices the whole queue (``queue.splice(0)``) and stamps each event
  with a fresh ``uuid`` (``random_uuid`` — the TS ``crypto.randomUUID()``) and the current
  ``session_id``. Empty queue → ``[]`` (no allocation).
- The event payloads are plain ``dict`` envelopes (``TypedDict`` for shape only); per the implementation
  rules these stay dicts and keep their wire keys.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from tabvis.bootstrap.state import (
    get_is_non_interactive_session,
    get_session_id,
)
from tabvis.types.tools import SdkWorkflowProgress
from tabvis.utils.crypto import random_uuid


class _Usage(TypedDict):
    total_tokens: int
    tool_uses: int
    duration_ms: int


class TaskStartedEvent(TypedDict, total=False):
    type: Literal["system"]
    subtype: Literal["task_started"]
    task_id: str
    tool_use_id: str
    description: str
    task_type: str
    workflow_name: str
    prompt: str


class TaskProgressEvent(TypedDict, total=False):
    type: Literal["system"]
    subtype: Literal["task_progress"]
    task_id: str
    tool_use_id: str
    description: str
    usage: _Usage
    last_tool_name: str
    summary: str
    # Delta batch of workflow state changes. Clients upsert by ``${type}:${index}`` then group by
    # phaseIndex to rebuild the phase tree.
    workflow_progress: list[SdkWorkflowProgress]


class TaskNotificationSdkEvent(TypedDict, total=False):
    type: Literal["system"]
    subtype: Literal["task_notification"]
    task_id: str
    tool_use_id: str
    status: Literal["completed", "failed", "stopped"]
    output_file: str
    summary: str
    usage: _Usage


class SessionStateChangedEvent(TypedDict):
    type: Literal["system"]
    subtype: Literal["session_state_changed"]
    state: Literal["idle", "running", "requires_action"]


# Discriminated on ``subtype``. Plain dict envelopes (wire keys preserved).
SdkEvent = (
    TaskStartedEvent
    | TaskProgressEvent
    | TaskNotificationSdkEvent
    | SessionStateChangedEvent
)

MAX_QUEUE_SIZE = 1000
_queue: list[SdkEvent] = []


def enqueue_sdk_event(event: SdkEvent) -> None:
    """Append an SDK event (no-op in interactive sessions; drops oldest on overflow)."""
    # SDK events are only consumed (drained) in headless/streaming mode. In TUI mode they would
    # accumulate up to the cap and never be read.
    if not get_is_non_interactive_session():
        return
    if len(_queue) >= MAX_QUEUE_SIZE:
        _queue.pop(0)
    _queue.append(event)


def drain_sdk_events() -> list[dict]:
    """Drain the queue, stamping each event with a fresh ``uuid`` and the ``session_id``."""
    if len(_queue) == 0:
        return []
    events = _queue[:]
    _queue.clear()
    return [
        {**e, "uuid": random_uuid(), "session_id": str(get_session_id())}
        for e in events
    ]


def emit_task_terminated_sdk(
    task_id: str,
    status: Literal["completed", "failed", "stopped"],
    *,
    tool_use_id: str | None = None,
    summary: str | None = None,
    output_file: str | None = None,
    usage: _Usage | None = None,
) -> None:
    """Emit a ``task_notification`` SDK event for a task reaching a terminal state.

    ``register_task`` always emits ``task_started``; this is the closing bookend. Call this from
    any exit path that sets a task terminal WITHOUT going through the XML notification parser (so
    paths that do both don't double-emit). Paths that suppress the XML notification (``notified``
    pre-set, kill paths, abort branches) must call this directly so SDK consumers see the task
    close.
    """
    # The TS object literal sets ``tool_use_id``/``usage`` to ``undefined`` when not provided;
    # those keys drop out at JSON serialization. We omit ``None``-valued optionals so the wire
    # envelope matches that JSON shape exactly.
    event: TaskNotificationSdkEvent = {
        "type": "system",
        "subtype": "task_notification",
        "task_id": task_id,
        "status": status,
        "output_file": output_file if output_file is not None else "",
        "summary": summary if summary is not None else "",
    }
    if tool_use_id is not None:
        event["tool_use_id"] = tool_use_id
    if usage is not None:
        event["usage"] = usage
    enqueue_sdk_event(event)
