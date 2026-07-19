"""Async hook registry

A module-level registry of in-flight async hooks (``async: true`` responses). The registry tracks
background command objects, polls for completion, parses their final sync JSON line, finalizes
(emit response + cleanup), and supports cancellation. Command objects are duck-typed as ``Any``;
the registry only touches ``.status`` / ``.result`` / ``.taskOutput`` / ``.cleanup()`` / ``.kill()``.
The hook-event union types are likewise represented as plain strings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.hooks.hook_events import emit_hook_response, start_hook_progress_interval
from tabvis.utils.session_environment import invalidate_session_env_cache
from tabvis.utils.slow_operations import json_parse, json_stringify

# HookEvent | 'StatusLine' | 'FileSuggestion' — plain string in the runtime.
HookEvent = str


@dataclass
class PendingAsyncHook:
    """An in-flight async hook record."""

    process_id: str
    hook_id: str
    hook_name: str
    hook_event: str
    start_time: int
    timeout: int
    command: str
    response_attachment_sent: bool
    stop_progress_interval: Callable[[], None]
    tool_name: str | None = None
    shell_command: Any | None = None


# Global registry state
_pending_hooks: dict[str, PendingAsyncHook] = {}


def register_pending_async_hook(
    *,
    process_id: str,
    hook_id: str,
    async_response: dict[str, Any],
    hook_name: str,
    hook_event: str,
    command: str,
    shell_command: Any,
    tool_name: str | None = None,
) -> None:
    """Register the pending async hook."""
    timeout = async_response.get("asyncTimeout") or 15000  # Default 15s
    log_for_debugging(
        f"Hooks: Registering async hook {process_id} ({hook_name}) with timeout {timeout}ms",
    )

    async def get_output() -> dict[str, str]:
        pending = _pending_hooks.get(process_id)
        task_output = pending.shell_command.taskOutput if pending and pending.shell_command else None
        if not task_output:
            return {"stdout": "", "stderr": "", "output": ""}
        stdout = await task_output.getStdout()
        stderr = task_output.getStderr()
        return {"stdout": stdout, "stderr": stderr, "output": stdout + stderr}

    stop_progress_interval = start_hook_progress_interval(
        hook_id=hook_id,
        hook_name=hook_name,
        hook_event=hook_event,
        get_output=get_output,
    )
    _pending_hooks[process_id] = PendingAsyncHook(
        process_id=process_id,
        hook_id=hook_id,
        hook_name=hook_name,
        hook_event=hook_event,
        tool_name=tool_name,
        command=command,
        start_time=_now_ms(),
        timeout=timeout,
        response_attachment_sent=False,
        shell_command=shell_command,
        stop_progress_interval=stop_progress_interval,
    )


def get_pending_async_hooks() -> list[PendingAsyncHook]:
    """Hooks whose response hasn't been delivered yet."""
    return [hook for hook in _pending_hooks.values() if not hook.response_attachment_sent]


async def _finalize_hook(
    hook: PendingAsyncHook,
    exit_code: int,
    outcome: str,
) -> None:
    """Stop progress, gather output, cleanup, emit the response."""
    hook.stop_progress_interval()
    task_output = hook.shell_command.taskOutput if hook.shell_command else None
    stdout = await task_output.getStdout() if task_output else ""
    stderr = task_output.getStderr() if task_output else ""
    if hook.shell_command:
        hook.shell_command.cleanup()
    emit_hook_response(
        {
            "hookId": hook.hook_id,
            "hookName": hook.hook_name,
            "hookEvent": hook.hook_event,
            "output": stdout + stderr,
            "stdout": stdout,
            "stderr": stderr,
            "exitCode": exit_code,
            "outcome": outcome,
        }
    )


async def check_for_async_hook_responses() -> list[dict[str, Any]]:
    """Check the for async hook responses.

    Snapshot the registry, check each hook's shell status, parse the final sync JSON line, finalize
    completed hooks and return their payloads. Failures are isolated (``allSettled`` semantics) so
    one throwing callback doesn't orphan already-applied side effects.
    """
    responses: list[dict[str, Any]] = []

    pending_count = len(_pending_hooks)
    log_for_debugging(f"Hooks: Found {pending_count} total hooks in registry")

    # Snapshot hooks before processing — we'll mutate the map after.
    hooks = list(_pending_hooks.values())

    async def _process(hook: PendingAsyncHook) -> dict[str, Any]:
        task_output = hook.shell_command.taskOutput if hook.shell_command else None
        stdout = (await task_output.getStdout()) if task_output else ""
        stderr = task_output.getStderr() if task_output else ""
        log_for_debugging(
            f"Hooks: Checking hook {hook.process_id} ({hook.hook_name}) - "
            f"attachmentSent: {hook.response_attachment_sent}, stdout length: {len(stdout)}",
        )

        if not hook.shell_command:
            log_for_debugging(
                f"Hooks: Hook {hook.process_id} has no shell command, removing from registry",
            )
            hook.stop_progress_interval()
            return {"type": "remove", "processId": hook.process_id}

        log_for_debugging(f"Hooks: Hook shell status {hook.shell_command.status}")

        if hook.shell_command.status == "killed":
            log_for_debugging(
                f"Hooks: Hook {hook.process_id} is {hook.shell_command.status}, "
                "removing from registry",
            )
            hook.stop_progress_interval()
            hook.shell_command.cleanup()
            return {"type": "remove", "processId": hook.process_id}

        if hook.shell_command.status != "completed":
            return {"type": "skip"}

        if hook.response_attachment_sent or not stdout.strip():
            log_for_debugging(
                f"Hooks: Skipping hook {hook.process_id} - already delivered/sent or no stdout",
            )
            hook.stop_progress_interval()
            return {"type": "remove", "processId": hook.process_id}

        lines = stdout.split("\n")
        log_for_debugging(
            f"Hooks: Processing {len(lines)} lines of stdout for {hook.process_id}",
        )

        exec_result = await hook.shell_command.result
        exit_code = exec_result.code

        response: dict[str, Any] = {}
        for line in lines:
            if line.strip().startswith("{"):
                log_for_debugging(f"Hooks: Found JSON line: {line.strip()[:100]}...")
                try:
                    parsed = json_parse(line.strip())
                    if "async" not in parsed:
                        log_for_debugging(
                            f"Hooks: Found sync response from {hook.process_id}: "
                            f"{json_stringify(parsed)}",
                        )
                        response = parsed
                        break
                except Exception:  # noqa: BLE001 - faithful to the TS empty catch
                    log_for_debugging(
                        f"Hooks: Failed to parse JSON from {hook.process_id}: {line.strip()}",
                    )

        hook.response_attachment_sent = True
        await _finalize_hook(hook, exit_code, "success" if exit_code == 0 else "error")

        return {
            "type": "response",
            "processId": hook.process_id,
            "isSessionStart": hook.hook_event == "SessionStart",
            "payload": {
                "processId": hook.process_id,
                "response": response,
                "hookName": hook.hook_name,
                "hookEvent": hook.hook_event,
                "toolName": hook.tool_name,
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": exit_code,
            },
        }

    settled = await asyncio.gather(
        *(_process(hook) for hook in hooks),
        return_exceptions=True,
    )

    # allSettled — isolate failures so one throwing callback doesn't orphan
    # already-applied side effects (responseAttachmentSent, finalizeHook) from others.
    session_start_completed = False
    for s in settled:
        if isinstance(s, BaseException):
            log_for_debugging(
                f"Hooks: checkForAsyncHookResponses callback rejected: {s}",
                {"level": "error"},
            )
            continue
        r = s
        if r["type"] == "remove":
            _pending_hooks.pop(r["processId"], None)
        elif r["type"] == "response":
            responses.append(r["payload"])
            _pending_hooks.pop(r["processId"], None)
            if r["isSessionStart"]:
                session_start_completed = True

    if session_start_completed:
        log_for_debugging("Invalidating session env cache after SessionStart hook completed")
        invalidate_session_env_cache()

    log_for_debugging(f"Hooks: checkForNewResponses returning {len(responses)} responses")
    return responses


def remove_delivered_async_hooks(process_ids: list[str]) -> None:
    """Remove the delivered async hooks."""
    for process_id in process_ids:
        hook = _pending_hooks.get(process_id)
        if hook and hook.response_attachment_sent:
            log_for_debugging(f"Hooks: Removing delivered hook {process_id}")
            hook.stop_progress_interval()
            _pending_hooks.pop(process_id, None)


async def finalize_pending_async_hooks() -> None:
    """Finalize completed hooks; kill + cancel the rest."""
    hooks = list(_pending_hooks.values())

    async def _finalize(hook: PendingAsyncHook) -> None:
        if hook.shell_command and hook.shell_command.status == "completed":
            result = await hook.shell_command.result
            await _finalize_hook(
                hook,
                result.code,
                "success" if result.code == 0 else "error",
            )
        else:
            if hook.shell_command and hook.shell_command.status != "killed":
                hook.shell_command.kill()
            await _finalize_hook(hook, 1, "cancelled")

    await asyncio.gather(*(_finalize(hook) for hook in hooks))
    _pending_hooks.clear()


def clear_all_async_hooks() -> None:
    """Test utility to clear all hooks."""
    for hook in _pending_hooks.values():
        hook.stop_progress_interval()
    _pending_hooks.clear()


def _now_ms() -> int:
    """``Date.now()`` — current epoch in milliseconds."""
    import time

    return int(time.time() * 1000)
