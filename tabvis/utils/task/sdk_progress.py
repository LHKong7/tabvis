"""Emit ``task_progress`` SDK events."""

from __future__ import annotations

import time

from tabvis.types.tools import SdkWorkflowProgress
from tabvis.utils.sdk_event_queue import enqueue_sdk_event


def emit_task_progress(
    *,
    task_id: str,
    tool_use_id: str | None,
    description: str,
    start_time: float,
    total_tokens: int,
    tool_uses: int,
    last_tool_name: str | None = None,
    summary: str | None = None,
    workflow_progress: list[SdkWorkflowProgress] | None = None,
) -> None:
    """Emit a ``task_progress`` SDK event.

    Shared by background agents (per ``tool_use`` in ``run_async_agent_lifecycle``) and workflows
    (per ``flush_progress`` batch). Accepts already-computed primitives so callers can derive them
    from their own state shapes.

    ``start_time`` is epoch milliseconds (matching the TS ``Date.now()`` usage); ``duration_ms``
    is ``now - start_time``.
    """
    enqueue_sdk_event(
        {
            "type": "system",
            "subtype": "task_progress",
            "task_id": task_id,
            "tool_use_id": tool_use_id,
            "description": description,
            "usage": {
                "total_tokens": total_tokens,
                "tool_uses": tool_uses,
                "duration_ms": int(time.time() * 1000) - int(start_time),
            },
            "last_tool_name": last_tool_name,
            "summary": summary,
            "workflow_progress": workflow_progress,
        }
    )
