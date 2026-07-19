"""Unified command queue

A single module-level command queue through which **all** commands flow — user input, task
notifications, orphaned permissions. In TS, React components subscribe via
``useSyncExternalStore`` (``subscribeToCommandQueue`` / ``getCommandQueueSnapshot``); non-React
code (the ``print.ts`` streaming loop) reads directly via ``get_command_queue()`` /
``get_command_queue_length()``.

Priority determines dequeue order: ``now`` > ``next`` > ``later``. Within the same priority,
commands are processed FIFO.

Casing convention (``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; the
:class:`~tabvis.types.text_input_types.QueuedCommand` queue entries keep their **camelCase wire
keys verbatim** (``orphanedPermission`` / ``pastedContents`` / ``preExpansionValue`` /
``skipSlashCommands`` / ``isMeta`` / ``agentId``) — they round-trip across the async queue
boundary into UserMessages / the transcript. This module never renames a wire key.

State model: the TS module holds module-level ``commandQueue`` / ``snapshot`` arrays + a
``createSignal()``. We replicate that with module-level globals + :func:`tabvis.utils.signal.
create_signal`. The frozen ``Object.freeze([...commandQueue])`` snapshot → an immutable
``tuple`` recreated on every mutation (reference changes only on mutation, as
``useSyncExternalStore`` requires).

Faithful-behavior notes:
- ``void recordQueueOperation(queueOp)`` is a best-effort fire-and-forget. Since
  ``record_queue_operation`` is ``async`` and this logging happens from sync queue mutators,
  it is scheduled on the running event loop when there is one; with no loop (the common
  headless/test path for a pure queue read/write) it is silently dropped — the queue mutation
  itself is fully synchronous and unaffected.
- ``extractTextContent`` is not provided by :mod:`tabvis.utils.messages`; a local helper is inlined
  below.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from datetime import UTC
from typing import Any, TypedDict

from tabvis.bootstrap.state import get_session_id
from tabvis.types.message_queue_types import QueueOperation, QueueOperationMessage
from tabvis.types.text_input_types import (
    PromptInputMode,
    QueuedCommand,
    QueuePriority,
)
from tabvis.utils.image_store import PastedContent
from tabvis.utils.object_group_by import object_group_by
from tabvis.utils.signal import create_signal

# ``SetAppState`` — the React state setter ``(f: (prev: AppState) => AppState) => void``.
SetAppState = Callable[[Callable[[Any], Any]], None]

# Anthropic content-block param — a plain dict (matches tabvis.types.command).
ContentBlockParam = dict[str, Any]


# =============================================================================================
# Logging helper
# =============================================================================================


def _now_iso() -> str:
    """``new Date().toISOString()`` — UTC, millisecond precision, trailing ``Z``."""
    # ``timespec='milliseconds'`` matches JS's fixed 3-digit millisecond fraction.
    from datetime import datetime

    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _record_queue_operation_fire_and_forget(queue_op: QueueOperationMessage) -> None:
    """Best-effort ``void recordQueueOperation(queueOp)`` (cyclic sibling, lazily imported).

    ``session_storage`` imports this module's sibling types; it is imported lazily here so this
    module's import-smoke passes regardless. Scheduled on the running loop when present; with no
    running loop the op is dropped (the synchronous queue mutation already happened).
    """

    async def _run() -> None:
        try:
            from tabvis.utils.session_storage import (  # noqa: PLC0415 (lazy cycle break)
                record_queue_operation,
            )

            await record_queue_operation(dict(queue_op))
        except Exception:  # noqa: BLE001 - best-effort logging, never disrupt the queue
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running (pure sync caller / tests) — drop the log, like `void` would
        # leave a rejected promise unobserved.
        return
    task = loop.create_task(_run())
    task.add_done_callback(_swallow_task_error)


def _swallow_task_error(task: asyncio.Task) -> None:
    try:
        task.result()
    except Exception:  # noqa: BLE001
        pass


def log_operation(operation: QueueOperation, content: str | None = None) -> None:
    """Build a :class:`QueueOperationMessage` and fire-and-forget ``record_queue_operation``."""
    session_id = get_session_id()
    queue_op: QueueOperationMessage = {
        "type": "queue-operation",
        "operation": operation,
        "timestamp": _now_iso(),
        "sessionId": session_id,
    }
    # ``...(content !== undefined && { content })`` — only spread when content is provided.
    if content is not None:
        queue_op["content"] = content
    _record_queue_operation_fire_and_forget(queue_op)


# =============================================================================================
# Unified command queue (module-level, independent of React state)
# =============================================================================================

_command_queue: list[QueuedCommand] = []
# Frozen snapshot — recreated on every mutation for useSyncExternalStore.
_snapshot: tuple[QueuedCommand, ...] = ()
_queue_changed = create_signal()


def _notify_subscribers() -> None:
    global _snapshot
    _snapshot = tuple(_command_queue)
    _queue_changed.emit()


# =============================================================================================
# useSyncExternalStore interface
# =============================================================================================

# Subscribe to command queue changes. Compatible with React's useSyncExternalStore.
subscribe_to_command_queue = _queue_changed.subscribe


def get_command_queue_snapshot() -> tuple[QueuedCommand, ...]:
    """Current snapshot of the command queue (frozen; changes reference only on mutation)."""
    return _snapshot


# =============================================================================================
# Read operations (for non-React code)
# =============================================================================================


def get_command_queue() -> list[QueuedCommand]:
    """A mutable copy of the current queue (one-off reads needing the actual commands)."""
    return list(_command_queue)


def get_command_queue_length() -> int:
    """The current queue length without copying."""
    return len(_command_queue)


def has_commands_in_queue() -> bool:
    """Whether there are commands in the queue."""
    return len(_command_queue) > 0


def recheck_command_queue() -> None:
    """Trigger a re-check by notifying subscribers (after async processing completes)."""
    if len(_command_queue) > 0:
        _notify_subscribers()


# =============================================================================================
# Write operations
# =============================================================================================


def enqueue(command: QueuedCommand) -> None:
    """Add a command to the queue (user-initiated). Defaults priority to ``next``."""
    _command_queue.append({**command, "priority": command.get("priority") or "next"})
    _notify_subscribers()
    value = command.get("value")
    log_operation("enqueue", value if isinstance(value, str) else None)


def enqueue_pending_notification(command: QueuedCommand) -> None:
    """Add a task notification. Defaults priority to ``later`` so user input isn't starved."""
    _command_queue.append({**command, "priority": command.get("priority") or "later"})
    _notify_subscribers()
    value = command.get("value")
    log_operation("enqueue", value if isinstance(value, str) else None)


PRIORITY_ORDER: dict[QueuePriority, int] = {
    "now": 0,
    "next": 1,
    "later": 2,
}


def _command_priority(cmd: QueuedCommand) -> int:
    """``PRIORITY_ORDER[cmd.priority ?? 'next']`` — note ``??`` keeps a falsy-but-present value."""
    priority = cmd.get("priority")
    if priority is None:
        priority = "next"
    return PRIORITY_ORDER[priority]


def dequeue(
    filter: Callable[[QueuedCommand], bool] | None = None,
) -> QueuedCommand | None:
    """Remove and return the highest-priority command, or ``None`` if empty.

    Within the same priority level, commands are dequeued FIFO. An optional ``filter`` narrows
    the candidates: only commands for which the predicate returns ``True`` are considered;
    non-matching commands stay in the queue untouched.
    """
    if len(_command_queue) == 0:
        return None

    best_idx = -1
    best_priority = float("inf")
    for i in range(len(_command_queue)):
        cmd = _command_queue[i]
        if filter is not None and not filter(cmd):
            continue
        priority = _command_priority(cmd)
        if priority < best_priority:
            best_idx = i
            best_priority = priority

    if best_idx == -1:
        return None

    dequeued = _command_queue.pop(best_idx)
    _notify_subscribers()
    log_operation("dequeue")
    return dequeued


def dequeue_all() -> list[QueuedCommand]:
    """Remove and return all commands from the queue. Logs a ``dequeue`` for each."""
    if len(_command_queue) == 0:
        return []

    commands = list(_command_queue)
    _command_queue.clear()
    _notify_subscribers()

    for _cmd in commands:
        log_operation("dequeue")

    return commands


def peek(
    filter: Callable[[QueuedCommand], bool] | None = None,
) -> QueuedCommand | None:
    """Highest-priority command without removing it, or ``None`` if empty.

    Accepts an optional ``filter`` — only commands passing the predicate are considered.
    """
    if len(_command_queue) == 0:
        return None
    best_idx = -1
    best_priority = float("inf")
    for i in range(len(_command_queue)):
        cmd = _command_queue[i]
        if filter is not None and not filter(cmd):
            continue
        priority = _command_priority(cmd)
        if priority < best_priority:
            best_idx = i
            best_priority = priority
    if best_idx == -1:
        return None
    return _command_queue[best_idx]


def dequeue_all_matching(
    predicate: Callable[[QueuedCommand], bool],
) -> list[QueuedCommand]:
    """Remove and return all commands matching ``predicate``, preserving priority order.

    Non-matching commands stay in the queue.
    """
    matched: list[QueuedCommand] = []
    remaining: list[QueuedCommand] = []
    for cmd in _command_queue:
        if predicate(cmd):
            matched.append(cmd)
        else:
            remaining.append(cmd)
    if len(matched) == 0:
        return []
    _command_queue.clear()
    _command_queue.extend(remaining)
    _notify_subscribers()
    for _cmd in matched:
        log_operation("dequeue")
    return matched


def remove(commands_to_remove: list[QueuedCommand]) -> None:
    """Remove specific commands from the queue by **reference identity**.

    Callers must pass the same object references that are in the queue. Logs a ``remove`` for
    each.
    """
    if len(commands_to_remove) == 0:
        return

    before = len(_command_queue)
    for i in range(len(_command_queue) - 1, -1, -1):
        if any(_command_queue[i] is c for c in commands_to_remove):
            del _command_queue[i]

    if len(_command_queue) != before:
        _notify_subscribers()

    for _cmd in commands_to_remove:
        log_operation("remove")


def remove_by_filter(
    predicate: Callable[[QueuedCommand], bool],
) -> list[QueuedCommand]:
    """Remove commands matching ``predicate``. Returns the removed commands (in queue order)."""
    removed: list[QueuedCommand] = []
    for i in range(len(_command_queue) - 1, -1, -1):
        if predicate(_command_queue[i]):
            removed.insert(0, _command_queue.pop(i))

    if len(removed) > 0:
        _notify_subscribers()
        for _cmd in removed:
            log_operation("remove")

    return removed


def clear_command_queue() -> None:
    """Clear all commands from the queue (ESC cancellation discards queued notifications)."""
    if len(_command_queue) == 0:
        return
    _command_queue.clear()
    _notify_subscribers()


def reset_command_queue() -> None:
    """Clear all commands and reset snapshot. Used for test cleanup."""
    global _snapshot
    _command_queue.clear()
    _snapshot = ()


# =============================================================================================
# Editable mode helpers
# =============================================================================================

_NON_EDITABLE_MODES: set[PromptInputMode] = {
    "task-notification",
}


def is_prompt_input_mode_editable(mode: PromptInputMode) -> bool:
    """Whether ``mode`` is an :data:`EditablePromptInputMode` (not a ``-notification`` mode)."""
    return mode not in _NON_EDITABLE_MODES


def is_queued_command_editable(cmd: QueuedCommand) -> bool:
    """Whether this queued command can be pulled into the input buffer via UP/ESC.

    System-generated commands (task, plan verification, channel messages) contain raw XML and
    must not leak into the user's input.
    """
    return is_prompt_input_mode_editable(cmd["mode"]) and not cmd.get("isMeta")


def is_queued_command_visible(cmd: QueuedCommand) -> bool:
    """Whether this queued command should render in the queue preview under the prompt.

    Superset of editable — channel messages show (so the keyboard user sees what arrived) but
    stay non-editable (raw XML). The TS gate ``(false || false) && ...`` is dead in this tree,
    so this collapses to :func:`is_queued_command_editable`.
    """
    return is_queued_command_editable(cmd)


# faithfully here (filter ``type == 'text'`` blocks, join their ``text``). Re-import from
# ``tabvis.utils.messages`` once it lands.
def _extract_text_content(
    blocks: Iterable[dict[str, Any]], separator: str = ""
) -> str:
    return separator.join(b["text"] for b in blocks if b.get("type") == "text")


def _extract_text_from_value(value: str | list[ContentBlockParam]) -> str:
    """Extract text from a queued command value (string → itself; blocks → text blocks joined)."""
    return value if isinstance(value, str) else _extract_text_content(value, "\n")


def _extract_images_from_value(
    value: str | list[ContentBlockParam],
    start_id: int,
) -> list[PastedContent]:
    """Extract base64 images from ``ContentBlockParam[]`` → :data:`PastedContent` format.

    Returns an empty list for string values or if no images found.
    """
    if isinstance(value, str):
        return []

    images: list[PastedContent] = []
    image_index = 0
    for block in value:
        source = block.get("source") or {}
        if block.get("type") == "image" and source.get("type") == "base64":
            images.append(
                {
                    "id": start_id + image_index,
                    "type": "image",
                    "content": source.get("data"),
                    "mediaType": source.get("media_type"),
                    "filename": f"image{image_index + 1}",
                }
            )
            image_index += 1
    return images


class PopAllEditableResult(TypedDict):
    """Result of :func:`pop_all_editable`."""

    text: str
    cursorOffset: int
    images: list[PastedContent]


def pop_all_editable(
    current_input: str,
    current_cursor_offset: int,
) -> PopAllEditableResult | None:
    """Pop all editable commands and combine them with current input for editing.

    Notification modes (``task-notification``) are left in the queue to be auto-processed later.
    Returns ``{text, cursorOffset, images}`` or ``None`` if no editable commands in queue.
    """
    if len(_command_queue) == 0:
        return None

    grouped = object_group_by(
        list(_command_queue),
        lambda cmd, _i: "editable"
        if is_queued_command_editable(cmd)
        else "non_editable",
    )
    editable = grouped.get("editable", [])
    non_editable = grouped.get("non_editable", [])

    if len(editable) == 0:
        return None

    # Extract text from queued commands (handles both strings and ContentBlockParam[]).
    queued_texts = [_extract_text_from_value(cmd["value"]) for cmd in editable]
    new_input = "\n".join(t for t in [*queued_texts, current_input] if t)

    # Cursor offset: length of joined queued commands + 1 + current cursor offset.
    cursor_offset = len("\n".join(queued_texts)) + 1 + current_cursor_offset

    # Extract images from queued commands.
    images: list[PastedContent] = []
    next_image_id = int(time.time() * 1000)  # Date.now() — ms timestamp as a unique-ID base.
    for cmd in editable:
        # handlePromptSubmit queues images in pastedContents (value is a string). Preserve the
        # original PastedContent id so imageStore lookups still work.
        pasted = cmd.get("pastedContents")
        if pasted:
            for content in pasted.values():
                if content.get("type") == "image":
                    images.append(content)
        # Bridge/remote commands may embed images directly in ContentBlockParam[].
        cmd_images = _extract_images_from_value(cmd["value"], next_image_id)
        images.extend(cmd_images)
        next_image_id += len(cmd_images)

    for command in editable:
        value = command.get("value")
        log_operation("popAll", value if isinstance(value, str) else None)

    # Replace queue contents with only the non-editable commands.
    _command_queue.clear()
    _command_queue.extend(non_editable)
    _notify_subscribers()

    return {"text": new_input, "cursorOffset": cursor_offset, "images": images}


# =============================================================================================
# Backward-compatible aliases (deprecated — prefer new names)
# =============================================================================================

# @deprecated Use subscribe_to_command_queue
subscribe_to_pending_notifications = subscribe_to_command_queue


def get_pending_notifications_snapshot() -> tuple[QueuedCommand, ...]:
    """@deprecated Use :func:`get_command_queue_snapshot`."""
    return _snapshot


# @deprecated Use has_commands_in_queue
has_pending_notifications = has_commands_in_queue

# @deprecated Use get_command_queue_length
get_pending_notifications_count = get_command_queue_length

# @deprecated Use recheck_command_queue
recheck_pending_notifications = recheck_command_queue


def dequeue_pending_notification() -> QueuedCommand | None:
    """@deprecated Use :func:`dequeue`."""
    return dequeue()


# @deprecated Use reset_command_queue
reset_pending_notifications = reset_command_queue

# @deprecated Use clear_command_queue
clear_pending_notifications = clear_command_queue


def get_commands_by_max_priority(
    max_priority: QueuePriority,
) -> list[QueuedCommand]:
    """Commands at or above a given priority level without removing them.

    Priority order: ``now`` (0) > ``next`` (1) > ``later`` (2). Passing ``now`` returns only
    now-priority commands; ``later`` returns everything.
    """
    threshold = PRIORITY_ORDER[max_priority]
    return [cmd for cmd in _command_queue if _command_priority(cmd) <= threshold]


def is_slash_command(cmd: QueuedCommand) -> bool:
    """Whether ``cmd`` is a slash command that should route through ``processSlashCommand``.

    Commands with ``skipSlashCommands`` (e.g. external messages) are NOT treated as slash
    commands — their text is meant for the model.
    """
    value = cmd.get("value")
    return (
        isinstance(value, str)
        and value.strip().startswith("/")
        and not cmd.get("skipSlashCommands")
    )
