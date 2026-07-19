"""Conversation context assembly.

Builds the system context block prepended to each conversation and cached for its duration.
``get_system_context`` carries the git-status snapshot. Project ``TABVIS.md`` instructions and
persistent auto-memory are assembled separately as dynamic system-prompt sections.

Memoization: the zero-arg async resolvers are wrapped with
:func:`tabvis.utils.memoize.memoize_with_ttl_async`, which exposes a ``.cache.clear()`` surface.
``get_git_status`` is an async resolver memoized the same way.

Casing: Python identifiers are snake_case; the returned context dict keeps the ``gitStatus`` wire
key expected by system-prompt assembly.

``get_branch``/``get_default_branch``/``git_exe`` live in :mod:`tabvis.utils.git_filesystem`.
"""

from __future__ import annotations

import os
import time

from tabvis.utils.diag_logs import log_for_diagnostics_no_pii
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.exec_file_no_throw import exec_file_no_throw
from tabvis.utils.git import get_is_git
from tabvis.utils.git_filesystem import get_branch, get_default_branch, git_exe
from tabvis.utils.git_settings import should_include_git_instructions
from tabvis.utils.log import log_error
from tabvis.utils.memoize import memoize_with_ttl_async

MAX_STATUS_CHARS = 2000


def _now_ms() -> int:
    """Wall-clock time in milliseconds."""
    return int(time.time() * 1000)


async def _get_git_status() -> str | None:
    if os.environ.get("NODE_ENV") == "test":
        # Avoid cycles in tests.
        return None

    start_time = _now_ms()
    log_for_diagnostics_no_pii("info", "git_status_started")

    is_git_start = _now_ms()
    is_git = get_is_git()
    log_for_diagnostics_no_pii(
        "info",
        "git_is_git_check_completed",
        {"duration_ms": _now_ms() - is_git_start, "is_git": is_git},
    )

    if not is_git:
        log_for_diagnostics_no_pii(
            "info",
            "git_status_skipped_not_git",
            {"duration_ms": _now_ms() - start_time},
        )
        return None

    try:
        git_cmds_start = _now_ms()
        branch = await get_branch()
        main_branch = await get_default_branch()
        status = (
            await exec_file_no_throw(
                git_exe(),
                ["--no-optional-locks", "status", "--short"],
                {"preserve_output_on_error": False},
            )
        )["stdout"].strip()
        log = (
            await exec_file_no_throw(
                git_exe(),
                ["--no-optional-locks", "log", "--oneline", "-n", "5"],
                {"preserve_output_on_error": False},
            )
        )["stdout"].strip()
        user_name = (
            await exec_file_no_throw(
                git_exe(),
                ["config", "user.name"],
                {"preserve_output_on_error": False},
            )
        )["stdout"].strip()

        log_for_diagnostics_no_pii(
            "info",
            "git_commands_completed",
            {"duration_ms": _now_ms() - git_cmds_start, "status_length": len(status)},
        )

        # Check if status exceeds character limit.
        truncated_status = (
            status[:MAX_STATUS_CHARS]
            + '\n... (truncated because it exceeds 2k characters. If you need more information, run "git status" using BashTool)'
            if len(status) > MAX_STATUS_CHARS
            else status
        )

        log_for_diagnostics_no_pii(
            "info",
            "git_status_completed",
            {
                "duration_ms": _now_ms() - start_time,
                "truncated": len(status) > MAX_STATUS_CHARS,
            },
        )

        parts = [
            "This is the git status at the start of the conversation. Note that this status is a snapshot in time, and will not update during the conversation.",
            f"Current branch: {branch}",
            f"Main branch (you will usually use this for PRs): {main_branch}",
            *([f"Git user: {user_name}"] if user_name else []),
            f"Status:\n{truncated_status or '(clean)'}",
            f"Recent commits:\n{log}",
        ]
        return "\n\n".join(parts)
    except Exception as error:  # noqa: BLE001 - any git error degrades to no status
        log_for_diagnostics_no_pii(
            "error",
            "git_status_failed",
            {"duration_ms": _now_ms() - start_time},
        )
        log_error(error)
        return None


get_git_status = memoize_with_ttl_async(_get_git_status)


async def _get_system_context() -> dict[str, str]:
    """Context prepended to each conversation, cached for its duration."""
    start_time = _now_ms()
    log_for_diagnostics_no_pii("info", "system_context_started")

    # Skip git status in CCR (unnecessary overhead on resume) or when git instructions
    # are disabled.
    git_status = (
        None
        if is_env_truthy(os.environ.get("TABVIS_REMOTE"))
        or not should_include_git_instructions()
        else await get_git_status()
    )

    # System prompt injection (for cache breaking) — currently disabled.
    injection = None

    log_for_diagnostics_no_pii(
        "info",
        "system_context_completed",
        {
            "duration_ms": _now_ms() - start_time,
            "has_git_status": git_status is not None,
            "has_injection": injection is not None,
        },
    )

    result: dict[str, str] = {}
    if git_status:
        result["gitStatus"] = git_status
    # cacheBreaker is never emitted: injection is currently always disabled above.
    return result


get_system_context = memoize_with_ttl_async(_get_system_context)
