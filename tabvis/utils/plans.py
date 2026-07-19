"""Plan file persistence

Plans are word-slugged markdown files (``{slug}.md`` for the main conversation,
``{slug}-agent-{agentId}.md`` for subagents) under the plans directory
(``settings.plansDirectory`` resolved against the project root, else
``<tabvis-config-home>/plans``). The slug is generated lazily per session and
cached in :data:`tabvis.bootstrap.state`'s plan-slug cache.

The module also handles plan recovery for resumed / forked sessions:
:func:`copy_plan_for_resume` (reuse slug, recover from file snapshot or message
history in remote/CCR sessions where local files don't persist) and
:func:`copy_plan_for_fork` (fresh slug, copy original content).

Casing: Python identifiers are snake_case; dict-shaped transcript messages keep
their camelCase wire keys (``snapshotFiles``, ``planContent``, ``parentUuid``,
etc.) verbatim.

stdlib substitutions (vs the TS deps):
- ``crypto.randomUUID`` -> ``tabvis.utils.crypto.random_uuid`` (existing).
- ``lodash-es/memoize`` -> a tiny module-level single-slot cache for
  :func:`get_plans_directory` (it takes no args, so lodash's keyed cache reduces
  to one slot). ``get_plans_directory.cache_clear`` is provided for tests.
- ``fs/promises`` ``copyFile``/``writeFile`` -> :mod:`asyncio` + stdlib
  ``shutil.copyfile`` / ``open``, run off-thread to keep the async signatures.
- ``path`` ``join``/``resolve``/``sep`` -> :mod:`os.path`.

The flat-tool constant ``EXIT_PLAN_MODE_V2_TOOL_NAME`` is imported from the flat
``tabvis.agent.tools.exit_plan_mode_tool`` module (it exists there).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import UTC
from typing import Any

from tabvis.bootstrap.state import get_plan_slug_cache, get_session_id
from tabvis.constants.tools import EXIT_PLAN_MODE_V2_TOOL_NAME
from tabvis.types.ids import AgentId, SessionId
from tabvis.types.logs import LogOption, SerializedMessage
from tabvis.utils.crypto import random_uuid
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.errors import is_enoent
from tabvis.utils.file_persistence.outputs_scanner import get_environment_kind
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.log import log_error
from tabvis.utils.settings.settings import get_initial_settings
from tabvis.utils.words import generate_word_slug

MAX_SLUG_RETRIES = 10


def get_plan_slug(session_id: SessionId | None = None) -> str:
    """Get or generate a word slug for the current session's plan.

    The slug is generated lazily on first access and cached for the session. If a
    plan file with the generated slug already exists, retries up to
    :data:`MAX_SLUG_RETRIES` times to avoid clobbering.
    """
    plan_id = session_id if session_id is not None else get_session_id()
    cache = get_plan_slug_cache()
    slug = cache.get(plan_id)
    if not slug:
        plans_dir = get_plans_directory()
        # Try to find a unique slug that doesn't conflict with existing files.
        for _ in range(MAX_SLUG_RETRIES):
            slug = generate_word_slug()
            file_path = os.path.join(plans_dir, f"{slug}.md")
            if not get_fs_implementation().exists_sync(file_path):
                break
        cache[plan_id] = slug  # type: ignore[assignment]
    return slug  # type: ignore[return-value]


def set_plan_slug(session_id: SessionId, slug: str) -> None:
    """Set a specific plan slug for a session (used when resuming a session)."""
    get_plan_slug_cache()[session_id] = slug


def clear_plan_slug(session_id: SessionId | None = None) -> None:
    """Clear the plan slug for the current session.

    Should be called on ``/clear`` to ensure a fresh plan file is used.
    """
    plan_id = session_id if session_id is not None else get_session_id()
    get_plan_slug_cache().pop(plan_id, None)


def clear_all_plan_slugs() -> None:
    """Clear ALL plan slug entries (all sessions). Used on ``/clear`` to free
    sub-session slug entries.
    """
    get_plan_slug_cache().clear()


# Sentinel for the single-slot memo cache below.
_UNSET: Any = object()
_plans_directory_cache: str = _UNSET


def get_plans_directory() -> str:
    """Resolve (and create) the plans directory for the session.

    Memoized: called from render bodies (FileRead/FileEdit/FileWrite UI) and
    permission checks. Inputs (initial settings + cwd) are fixed at startup, so
    the ``mkdir`` result is stable for the session; without memoization each
    rendered tool message would trigger a ``mkdir`` syscall.
    """
    global _plans_directory_cache
    if _plans_directory_cache is not _UNSET:
        return _plans_directory_cache

    settings = get_initial_settings()
    # ``plansDirectory`` is not a modeled SettingsJson field (extra="allow"); read
    # it off the model dynamically, keeping the camelCase wire key verbatim.
    settings_dir = getattr(settings, "plansDirectory", None)
    if settings_dir is None:
        extra = getattr(settings, "model_extra", None)
        if isinstance(extra, dict):
            settings_dir = extra.get("plansDirectory")

    if settings_dir:
        # settings.json value is relative to the project root.
        cwd = get_cwd()
        resolved = os.path.normpath(os.path.join(cwd, settings_dir))

        # Validate the path stays within the project root (prevent traversal).
        if not resolved.startswith(cwd + os.sep) and resolved != cwd:
            log_error(
                ValueError(
                    f"plansDirectory must be within project root: {settings_dir}"
                )
            )
            plans_path = os.path.join(get_tabvis_config_home_dir(), "plans")
        else:
            plans_path = resolved
    else:
        # Default.
        plans_path = os.path.join(get_tabvis_config_home_dir(), "plans")

    # Ensure the directory exists (mkdir_sync is recursive + a no-op if present).
    try:
        get_fs_implementation().mkdir_sync(plans_path)
    except Exception as error:  # noqa: BLE001 — faithful to TS catch-all + logError.
        log_error(error)

    _plans_directory_cache = plans_path
    return plans_path


def _get_plans_directory_cache_clear() -> None:
    """Reset the :func:`get_plans_directory` memo (test/maintenance helper)."""
    global _plans_directory_cache
    _plans_directory_cache = _UNSET


# Expose a ``.cache_clear`` attribute consistently with the other memoized helpers.
get_plans_directory.cache_clear = _get_plans_directory_cache_clear  # type: ignore[attr-defined]


def get_plan_file_path(agent_id: AgentId | None = None) -> str:
    """Get the file path for a session's plan.

    For the main conversation (no ``agent_id``): ``{plan_slug}.md``.
    For subagents (``agent_id`` given): ``{plan_slug}-agent-{agent_id}.md``.
    """
    plan_slug = get_plan_slug(get_session_id())

    if not agent_id:
        return os.path.join(get_plans_directory(), f"{plan_slug}.md")

    return os.path.join(get_plans_directory(), f"{plan_slug}-agent-{agent_id}.md")


def get_plan(agent_id: AgentId | None = None) -> str | None:
    """Get the plan content for a session (``None`` if the file is missing)."""
    file_path = get_plan_file_path(agent_id)
    try:
        return get_fs_implementation().read_file_sync(file_path, {"encoding": "utf-8"})
    except Exception as error:  # noqa: BLE001 — faithful to TS catch + isENOENT.
        if is_enoent(error):
            return None
        log_error(error)
        return None


def _get_slug_from_log(log: LogOption) -> str | None:
    """Extract the plan slug from a log's message history."""
    for message in log.get("messages", []):
        slug = message.get("slug")
        if slug:
            return slug
    return None


async def copy_plan_for_resume(
    log: LogOption,
    target_session_id: SessionId | None = None,
) -> bool:
    """Restore the plan slug from a resumed session.

    Sets the slug in the session cache so :func:`get_plan_slug` returns it. If the
    plan file is missing, attempts recovery from a file snapshot (written
    incrementally during the session) or from message history. Returns ``True`` if
    a plan file exists (or was recovered) for the slug.

    ``target_session_id`` should be the ORIGINAL session ID being resumed, not the
    temporary session ID from before resume.
    """
    slug = _get_slug_from_log(log)
    if not slug:
        return False

    # Set the slug for the target session ID (or current if not provided).
    session_id = target_session_id if target_session_id is not None else get_session_id()
    set_plan_slug(session_id, slug)

    # Attempt to read the plan file directly — recovery triggers on ENOENT.
    plan_path = os.path.join(get_plans_directory(), f"{slug}.md")
    try:
        await get_fs_implementation().read_file(plan_path, {"encoding": "utf-8"})
        return True
    except Exception as e:  # noqa: BLE001 — faithful to TS catch + isENOENT branch.
        if not is_enoent(e):
            # Don't throw — called fire-and-forget with no error handler.
            log_error(e)
            return False
        # Only attempt recovery in remote sessions (CCR) where files don't persist.
        if get_environment_kind() is None:
            return False

        log_for_debugging(
            f"Plan file missing during resume: {plan_path}. Attempting recovery."
        )

        # Try the file snapshot first (written incrementally during the session).
        snapshot_plan = _find_file_snapshot_entry(log.get("messages", []), "plan")
        recovered: str | None = None
        if snapshot_plan and len(snapshot_plan["content"]) > 0:
            recovered = snapshot_plan["content"]
            log_for_debugging(
                f"Plan recovered from file snapshot, {len(recovered)} chars",
                {"level": "info"},
            )
        else:
            # Fall back to searching the message history.
            recovered = _recover_plan_from_messages(log)
            if recovered:
                log_for_debugging(
                    f"Plan recovered from message history, {len(recovered)} chars",
                    {"level": "info"},
                )

        if recovered:
            try:
                await asyncio.to_thread(_write_text_file, plan_path, recovered)
                return True
            except Exception as write_error:  # noqa: BLE001 — faithful to TS catch.
                log_error(write_error)
                return False
        log_for_debugging(
            "Plan file recovery failed: no file snapshot or plan content found in "
            "message history"
        )
        return False


async def copy_plan_for_fork(
    log: LogOption,
    target_session_id: SessionId,
) -> bool:
    """Copy a plan file for a forked session.

    Unlike :func:`copy_plan_for_resume` (which reuses the original slug), this
    generates a NEW slug for the forked session and writes the original plan
    content to the new file. This prevents the original and forked sessions from
    clobbering each other's plan files.
    """
    original_slug = _get_slug_from_log(log)
    if not original_slug:
        return False

    plans_dir = get_plans_directory()
    original_plan_path = os.path.join(plans_dir, f"{original_slug}.md")

    # Generate a new slug for the forked session (do NOT reuse the original).
    new_slug = get_plan_slug(target_session_id)
    new_plan_path = os.path.join(plans_dir, f"{new_slug}.md")
    try:
        await asyncio.to_thread(shutil.copyfile, original_plan_path, new_plan_path)
        return True
    except Exception as error:  # noqa: BLE001 — faithful to TS catch + isENOENT.
        if is_enoent(error):
            return False
        log_error(error)
        return False


def _recover_plan_from_messages(log: LogOption) -> str | None:
    """Recover plan content from the message history.

    Plan content can appear in three forms depending on what happened during the
    session:

    1. ExitPlanMode tool_use input — ``normalizeToolInput`` injects the plan
       content into the tool_use input, which persists in the transcript.
    2. ``planContent`` field on user messages — set during the "clear context and
       implement" flow when ExitPlanMode is approved.
    3. ``plan_file_reference`` attachment — created by auto-compact to preserve
       the plan across compaction boundaries.
    """
    messages = log.get("messages", [])
    for i in range(len(messages) - 1, -1, -1):
        msg: Any = messages[i]
        if not msg:
            continue

        if msg.get("type") == "assistant":
            content = msg.get("message", {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") == EXIT_PLAN_MODE_V2_TOOL_NAME
                    ):
                        block_input = block.get("input")
                        plan = (
                            block_input.get("plan")
                            if isinstance(block_input, dict)
                            else None
                        )
                        if isinstance(plan, str) and len(plan) > 0:
                            return plan

        if msg.get("type") == "user":
            plan_content = msg.get("planContent")
            if isinstance(plan_content, str) and len(plan_content) > 0:
                return plan_content

        if msg.get("type") == "attachment":
            attachment = msg.get("attachment")
            if (
                isinstance(attachment, dict)
                and attachment.get("type") == "plan_file_reference"
            ):
                plan = attachment.get("planContent")
                if isinstance(plan, str) and len(plan) > 0:
                    return plan
    return None


def _find_file_snapshot_entry(
    messages: list[SerializedMessage],
    key: str,
) -> dict[str, Any] | None:
    """Find a file entry in the most recent file-snapshot system message.

    Scans backwards to find the latest snapshot, then returns the entry whose
    ``key`` matches.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg: Any = messages[i]
        if (
            msg
            and msg.get("type") == "system"
            and msg.get("subtype") == "file_snapshot"
            and "snapshotFiles" in msg
        ):
            files = msg.get("snapshotFiles") or []
            for f in files:
                if f.get("key") == key:
                    return f
            return None
    return None


async def persist_file_snapshot_if_remote() -> None:
    """Persist a snapshot of session files (plan, todos) to the transcript.

    Called incrementally whenever these files change. Only active in remote
    sessions (CCR) where local files don't persist between sessions.
    """
    if get_environment_kind() is None:
        return
    try:
        snapshot_files: list[dict[str, Any]] = []

        # Snapshot the plan file.
        plan = get_plan()
        if plan:
            snapshot_files.append(
                {
                    "key": "plan",
                    "path": get_plan_file_path(),
                    "content": plan,
                }
            )

        if len(snapshot_files) == 0:
            return

        # SystemFileSnapshotMessage — wire keys kept verbatim (camelCase).
        message: dict[str, Any] = {
            "type": "system",
            "subtype": "file_snapshot",
            "content": "File snapshot",
            "level": "info",
            "isMeta": True,
            "timestamp": _now_iso(),
            "uuid": random_uuid(),
            "snapshotFiles": snapshot_files,
        }

        # Lazy import to break the plans <-> session_storage cycle (TS dynamic
        # ``await import('./sessionStorage.js')``).
        from tabvis.utils.session_storage import record_transcript

        await record_transcript([message])
    except Exception as error:  # noqa: BLE001 — faithful to TS catch + logError.
        log_error(error)


def _write_text_file(path: str, content: str) -> None:
    """Write ``content`` to ``path`` as UTF-8 (off-thread helper)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _now_iso() -> str:
    """ISO-8601 timestamp with millisecond precision and a ``Z`` suffix.

    Mirrors JS ``new Date().toISOString()`` (e.g. ``2026-06-06T12:00:00.000Z``).
    """
    from datetime import datetime

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


__all__ = [
    "MAX_SLUG_RETRIES",
    "clear_all_plan_slugs",
    "clear_plan_slug",
    "copy_plan_for_fork",
    "copy_plan_for_resume",
    "get_plan",
    "get_plan_file_path",
    "get_plan_slug",
    "get_plans_directory",
    "persist_file_snapshot_if_remote",
    "set_plan_slug",
]
