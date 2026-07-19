"""Pre/PostToolUse hook engine

Hooks are user-defined shell commands run at points in Tabvis's lifecycle. This module implements the
PreToolUse / PostToolUse path of the TS engine for the headless skeleton:

* :func:`get_hooks_config` — the config source. Reads ``settings.hooks`` plus an optional
  ``TABVIS_HOOKS`` env-var JSON override (so the engine is testable and the gate is real).
  Registered/session-hook merge is not supported in this build.
* :func:`matches_pattern` — the matcher: ``"*"``/empty matches all; a ``[A-Za-z0-9_|]+`` string is
  an exact (or pipe-separated) match (with legacy-name normalization); otherwise a regex.
* :func:`get_matching_hooks` — pick the matchers for ``(event, tool_name)`` and flatten their
  command hooks.
* :func:`exec_command_hook` — run one hook's shell command via ``asyncio`` subprocess, piping the
  tool input as JSON on stdin (trailing newline, matching the TS sync path) with a timeout.
* :func:`parse_hook_output` / :func:`process_hook_json_output` — parse JSON stdout
  (``decision: approve|block``, ``reason``, ``systemMessage``, ``hookSpecificOutput`` →
  permission decision / updatedInput / additionalContext / continue).
* :func:`execute_hooks` — the driver: gate (``TABVIS_SIMPLE``), match, run each hook serially, and
  yield :class:`AggregatedHookResult`-shaped dicts.
* :func:`execute_pre_tool_hooks` / :func:`execute_post_tool_hooks` — build the typed hook input and
  delegate to :func:`execute_hooks`.

With NO configured hooks (clean env) :func:`execute_hooks` yields nothing — identical observable
behavior to the previous stub, but the engine is now real.

Not supported in this build: SessionStart/Stop/Notification/PreCompact/SubagentStop/… events, the
prompt/agent/http/callback/function hook types (only ``command`` is supported), async/background
hooks + the async-hook registry, the hook matcher cache, the ``if``-condition matcher, MCP-tool
hook events, ``$TABVIS_PROJECT_DIR`` expansion in commands, parallel-hook aggregation nuances
(precedence is preserved; we run serially), beta tracing / OTel / analytics, workspace-trust gating,
and PowerShell/Windows-bash shell selection (we use the platform default shell).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncGenerator
from datetime import UTC
from typing import Any

from tabvis.agent.api.client import get_session_id
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.errors import get_error_message
from tabvis.utils.log import log_error

# 10 minutes — the per-hook execution cap (TOOL_HOOK_EXECUTION_TIMEOUT_MS).
TOOL_HOOK_EXECUTION_TIMEOUT_MS = 10 * 60 * 1000

# Legacy → canonical tool-name aliases (src/utils/permissions/permissionRuleParser.ts). Only the
# string values are referenced here; inlined to avoid pulling the (unported) parser module.
_LEGACY_TOOL_NAME_ALIASES: dict[str, str] = {
    "Task": "Agent",
    "KillShell": "TaskStop",
    "AgentOutputTool": "TaskOutput",
    "BashOutputTool": "TaskOutput",
}


def normalize_legacy_tool_name(name: str) -> str:
    """Normalize the legacy tool name."""
    return _LEGACY_TOOL_NAME_ALIASES.get(name, name)


def get_legacy_tool_names(canonical_name: str) -> list[str]:
    """The legacy aliases that map to ``canonical_name``."""
    return [
        legacy
        for legacy, canonical in _LEGACY_TOOL_NAME_ALIASES.items()
        if canonical == canonical_name
    ]


# ----------------------------------------------------------------------------------------------
# Config source (STUB — settings not implemented)
# ----------------------------------------------------------------------------------------------


def get_hooks_config() -> dict[str, list[dict[str, Any]]]:
    """Return the per-event hook config: ``{event: [{matcher, hooks:[...]}, ...]}``.

    Reads the live config sources: the merged settings (``mergedSettings.hooks ?? {}``) plus a
    ``TABVIS_HOOKS`` env override. Registered (SDK) + session hooks are not supported in this build.
    With no configured ``settings.hooks`` and no env var this returns ``{}`` for a clean env.

    A ``TABVIS_HOOKS`` env var (JSON of the same ``{event: [matcher...]}`` shape) is honored so the
    engine is testable and the gate is real; its events are appended after the settings hooks for
    the same event. Malformed JSON is ignored (logged for debugging).

    Registered + session-hook merge and managed-hook gating are not supported in this build.
    """
    from tabvis.utils.settings.settings import get_initial_settings

    config: dict[str, list[dict[str, Any]]] = {}
    settings_hooks = get_initial_settings().hooks
    if settings_hooks:
        for event, matchers in settings_hooks.items():
            config[event] = list(matchers)

    raw = os.environ.get("TABVIS_HOOKS")
    if not raw:
        return config
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as error:  # noqa: BLE001 - tolerate bad env, fall through
        log_for_debugging(f"Failed to parse TABVIS_HOOKS env JSON: {error}")
        return config
    if not isinstance(parsed, dict):
        return config
    for event, matchers in parsed.items():
        config.setdefault(event, []).extend(matchers if isinstance(matchers, list) else [])
    return config


# ----------------------------------------------------------------------------------------------
# Matcher
# ----------------------------------------------------------------------------------------------

_SIMPLE_MATCHER = re.compile(r"^[a-zA-Z0-9_|]+$")


def matches_pattern(match_query: str, matcher: str) -> bool:
    """Whether ``match_query`` matches ``matcher``.

    ``""``/``"*"`` matches everything. A simple ``[A-Za-z0-9_|]+`` string is an exact match (or
    pipe-separated alternatives), comparing against the legacy-normalized matcher. Anything else
    is treated as a regex (also tested against legacy tool names so ``"^Task$"`` still matches).
    """
    if not matcher or matcher == "*":
        return True

    if _SIMPLE_MATCHER.match(matcher):
        if "|" in matcher:
            patterns = [normalize_legacy_tool_name(p.strip()) for p in matcher.split("|")]
            return match_query in patterns
        return match_query == normalize_legacy_tool_name(matcher)

    try:
        regex = re.compile(matcher)
    except re.error:
        log_for_debugging(f"Invalid regex pattern in hook matcher: {matcher}")
        return False

    if regex.search(match_query):
        return True
    for legacy_name in get_legacy_tool_names(match_query):
        if regex.search(legacy_name):
            return True
    return False


# ----------------------------------------------------------------------------------------------
# Base hook input and attachment-message helpers
# ----------------------------------------------------------------------------------------------


def create_base_hook_input(
    permission_mode: str | None = None,
    session_id: str | None = None,
    agent_info: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The fields common to all hook inputs.

    ``transcript_path`` is empty in the skeleton (session storage not implemented). The wire keys are
    snake_case (they round-trip to the hook command's stdin JSON).
    """
    resolved_session_id = session_id or get_session_id()
    agent_info = agent_info or {}
    return {
        "session_id": resolved_session_id,
        "transcript_path": "",
        "cwd": get_cwd(),
        "permission_mode": permission_mode,
        "agent_id": agent_info.get("agentId"),
        "agent_type": agent_info.get("agentType"),
    }


def create_attachment_message(attachment: dict[str, Any]) -> dict[str, Any]:
    """Wrap an attachment dict in an AttachmentMessage."""
    import uuid as _uuid
    from datetime import datetime

    return {
        "type": "attachment",
        "attachment": attachment,
        "uuid": str(_uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


# ----------------------------------------------------------------------------------------------
# Hook JSON output parsing
# ----------------------------------------------------------------------------------------------


def parse_hook_output(stdout: str) -> dict[str, Any]:
    """Parse the hook output.

    Returns one of ``{"plainText": str}``, ``{"json": dict}``, or
    ``{"plainText": str, "validationError": str}``. Output not starting with ``{`` is plain text.

    This accepts any JSON object shape (the fields it reads are optional anyway); strict schema
    validation and a schema-hint error message are not implemented in this build.
    """
    trimmed = stdout.strip()
    if not trimmed.startswith("{"):
        log_for_debugging("Hook output does not start with {, treating as plain text")
        return {"plainText": stdout}
    try:
        parsed = json.loads(trimmed)
    except (ValueError, TypeError) as error:  # noqa: BLE001 - fall back to plain text
        log_for_debugging(f"Failed to parse hook output as JSON: {error}")
        return {"plainText": stdout}
    if not isinstance(parsed, dict):
        return {"plainText": stdout}
    log_for_debugging("Successfully parsed hook JSON output")
    return {"json": parsed}


def is_async_hook_json_output(json_obj: dict[str, Any]) -> bool:
    """``{Async: true}``."""
    return isinstance(json_obj, dict) and json_obj.get("async") is True


def process_hook_json_output(
    json_obj: dict[str, Any],
    command: str,
    hook_name: str,
    tool_use_id: str,
    hook_event: str,
    expected_hook_event: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    exit_code: int | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    """Process the hook json output.

    Maps a sync hook JSON object to a partial :class:`HookResult`-shaped dict:
    ``continue:false`` → ``preventContinuation``/``stopReason``; ``decision:approve|block`` →
    ``permissionBehavior`` (+ ``blockingError`` on block); ``systemMessage``; and the
    ``hookSpecificOutput`` PreToolUse (``permissionDecision``/``updatedInput``/``additionalContext``)
    and PostToolUse (``additionalContext``/``updatedMCPToolOutput``) branches.
    """
    result: dict[str, Any] = {}

    if json_obj.get("continue") is False:
        result["preventContinuation"] = True
        if json_obj.get("stopReason"):
            result["stopReason"] = json_obj["stopReason"]

    decision = json_obj.get("decision")
    if decision:
        if decision == "approve":
            result["permissionBehavior"] = "allow"
        elif decision == "block":
            result["permissionBehavior"] = "deny"
            result["blockingError"] = {
                "blockingError": json_obj.get("reason") or "Blocked by hook",
                "command": command,
            }
        else:
            raise ValueError(
                f"Unknown hook decision type: {decision}. Valid types are: approve, block"
            )

    if json_obj.get("systemMessage"):
        result["systemMessage"] = json_obj["systemMessage"]

    hook_specific = json_obj.get("hookSpecificOutput")
    # Top-level PreToolUse permissionDecision (before the hookSpecificOutput switch).
    if (
        isinstance(hook_specific, dict)
        and hook_specific.get("hookEventName") == "PreToolUse"
        and hook_specific.get("permissionDecision")
    ):
        pd = hook_specific["permissionDecision"]
        if pd == "allow":
            result["permissionBehavior"] = "allow"
        elif pd == "deny":
            result["permissionBehavior"] = "deny"
            result["blockingError"] = {
                "blockingError": json_obj.get("reason") or "Blocked by hook",
                "command": command,
            }
        elif pd == "ask":
            result["permissionBehavior"] = "ask"
        else:
            raise ValueError(
                f"Unknown hook permissionDecision type: {pd}. Valid types are: allow, deny, ask"
            )

    if result.get("permissionBehavior") is not None and json_obj.get("reason") is not None:
        result["hookPermissionDecisionReason"] = json_obj["reason"]

    if isinstance(hook_specific, dict):
        event_name = hook_specific.get("hookEventName")
        if expected_hook_event and event_name != expected_hook_event:
            raise ValueError(
                f"Hook returned incorrect event name: expected '{expected_hook_event}' but got "
                f"'{event_name}'. Full stdout: {json.dumps(json_obj, indent=2)}"
            )

        if event_name == "PreToolUse":
            pd = hook_specific.get("permissionDecision")
            if pd == "allow":
                result["permissionBehavior"] = "allow"
            elif pd == "deny":
                result["permissionBehavior"] = "deny"
                result["blockingError"] = {
                    "blockingError": (
                        hook_specific.get("permissionDecisionReason")
                        or json_obj.get("reason")
                        or "Blocked by hook"
                    ),
                    "command": command,
                }
            elif pd == "ask":
                result["permissionBehavior"] = "ask"
            result["hookPermissionDecisionReason"] = hook_specific.get(
                "permissionDecisionReason"
            )
            if hook_specific.get("updatedInput"):
                result["updatedInput"] = hook_specific["updatedInput"]
            result["additionalContext"] = hook_specific.get("additionalContext")
        elif event_name in ("UserPromptSubmit", "Setup", "SubagentStart", "PostToolUseFailure"):
            result["additionalContext"] = hook_specific.get("additionalContext")
        elif event_name == "PostToolUse":
            result["additionalContext"] = hook_specific.get("additionalContext")
            if hook_specific.get("updatedMCPToolOutput"):
                result["updatedMCPToolOutput"] = hook_specific["updatedMCPToolOutput"]

    # Build the display message attachment.
    if result.get("blockingError"):
        result["message"] = create_attachment_message(
            {
                "type": "hook_blocking_error",
                "hookName": hook_name,
                "toolUseID": tool_use_id,
                "hookEvent": hook_event,
                "blockingError": result["blockingError"],
            }
        )
    else:
        result["message"] = create_attachment_message(
            {
                "type": "hook_success",
                "hookName": hook_name,
                "toolUseID": tool_use_id,
                "hookEvent": hook_event,
                # JSON-output hooks inject context via additionalContext, not this field.
                "content": "",
                "stdout": stdout,
                "stderr": stderr,
                "exitCode": exit_code,
                "command": command,
                "durationMs": duration_ms,
            }
        )
    return result


# ----------------------------------------------------------------------------------------------
# Command hook execution
# ----------------------------------------------------------------------------------------------


async def exec_command_hook(
    hook: dict[str, Any],
    hook_event: str,
    hook_name: str,
    json_input: str,
    timeout_ms: int,
) -> dict[str, Any]:
    """Run a ``command``-type hook's shell command.

    Spawns the command through the platform shell (``/bin/sh -c`` via
    ``asyncio.create_subprocess_shell``), pipes ``json_input + "\\n"`` on stdin, and reads stdout
    /stderr with a timeout. Returns ``{stdout, stderr, output, status, aborted?}``.

    Not supported in this build: PowerShell/Git-Bash shell selection, ``$TABVIS_PROJECT_DIR``/
    ``TABVIS_SHELL_PREFIX``/``TABVIS_ENV_FILE`` env wiring, async/background hooks + the async-hook
    registry, the streaming first-line async detection, and prompt-request handling on stdout.
    """
    command = hook.get("command", "")
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=get_cwd(),
            env=dict(os.environ),
        )
    except OSError as error:
        err = f"Error occurred while executing hook command: {get_error_message(error)}"
        return {"stdout": "", "stderr": err, "output": err, "status": 1}

    # Trailing newline matches the TS sync path (bash `read -r line` needs the delimiter).
    stdin_bytes = (json_input + "\n").encode("utf-8")
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes), timeout=timeout_ms / 1000
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return {
            "stdout": "",
            "stderr": "Hook cancelled",
            "output": "Hook cancelled",
            "status": 1,
            "aborted": True,
        }
    except (ValueError, OSError) as error:  # noqa: BLE001 - mirror the TS catch -> status 1
        err = f"Error occurred while executing hook command: {get_error_message(error)}"
        return {"stdout": "", "stderr": err, "output": err, "status": 1}

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    status = proc.returncode if proc.returncode is not None else 1
    return {"stdout": stdout, "stderr": stderr, "output": stdout + stderr, "status": status}


def get_hook_display_text(hook: dict[str, Any]) -> str:
    """Return the hook display text."""
    return hook.get("command", "") or hook.get("url", "") or hook.get("prompt", "")


# ----------------------------------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------------------------------

# MatchedHook = {hook, skillRoot?, hookSource?}


def get_matching_hooks(
    hook_event: str,
    hook_input: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return the hooks matching ``(hook_event, match_query)``.

    Bounded: reads matchers from :func:`get_hooks_config` only (no app-state/session/registered
    merge), supports the tool-event match query (``tool_name``) plus a couple of other events, and
    flattens only ``command`` hooks (other hook types are deferred). No dedup / ``if``-condition
    filtering (single-source config can't have cross-scope duplicates here).
    """
    try:
        hook_matchers = get_hooks_config().get(hook_event, []) or []

        match_query: str | None = None
        event_name = hook_input.get("hook_event_name")
        if event_name in (
            "PreToolUse",
            "PostToolUse",
            "PostToolUseFailure",
            "PermissionRequest",
            "PermissionDenied",
        ):
            match_query = hook_input.get("tool_name")
        elif event_name == "SessionStart":
            match_query = hook_input.get("source")
        elif event_name in ("Setup", "PreCompact", "PostCompact"):
            match_query = hook_input.get("trigger")
        elif event_name in ("SubagentStart", "SubagentStop"):
            match_query = hook_input.get("agent_type")

        log_for_debugging(
            f"Getting matching hook commands for {hook_event} with query: {match_query}"
        )

        if match_query:
            filtered = [
                m
                for m in hook_matchers
                if not m.get("matcher") or matches_pattern(match_query, m["matcher"])
            ]
        else:
            filtered = list(hook_matchers)

        matched: list[dict[str, Any]] = []
        for matcher in filtered:
            for hook in matcher.get("hooks", []) or []:
                matched.append({"hook": hook, "skillRoot": None, "hookSource": "settings"})

        log_for_debugging(f"Matched {len(matched)} hooks for query '{match_query or 'no match'}'")
        return matched
    except Exception:  # noqa: BLE001 - TS returns [] on any failure
        return []


# ----------------------------------------------------------------------------------------------
# Blocking-message formatters
# ----------------------------------------------------------------------------------------------


def get_pre_tool_hook_blocking_message(hook_name: str, blocking_error: dict[str, Any]) -> str:
    """Return the pre tool hook blocking message."""
    return f"{hook_name} hook error: {blocking_error.get('blockingError')}"


def get_stop_hook_message(blocking_error: dict[str, Any]) -> str:
    """Return the stop hook message."""
    return f"Stop hook feedback:\n{blocking_error.get('blockingError')}"


def get_user_prompt_submit_hook_blocking_message(blocking_error: dict[str, Any]) -> str:
    """Return the user prompt submit hook blocking message."""
    return f"UserPromptSubmit operation blocked by hook:\n{blocking_error.get('blockingError')}"


# ----------------------------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------------------------


async def execute_hooks(
    *,
    hook_input: dict[str, Any],
    tool_use_id: str,
    match_query: str | None = None,
    timeout_ms: int = TOOL_HOOK_EXECUTION_TIMEOUT_MS,
) -> AsyncGenerator[dict[str, Any], None]:
    """Common hook-execution driver.

    Gates on ``TABVIS_SIMPLE`` (skeleton's bare path runs no hooks), matches hooks for the event,
    yields a ``hook_progress`` progress message per hook, runs each ``command`` hook (serially —
    the parallel ``all()`` aggregation is deferred but precedence is preserved), and yields
    :class:`AggregatedHookResult`-shaped dicts: ``{message}``, ``{blockingError}``,
    ``{preventContinuation, stopReason}``, ``{additionalContexts}``, ``{permissionBehavior,
    hookPermissionDecisionReason, hookSource, updatedInput}``, ``{updatedInput}`` (passthrough),
    ``{updatedMCPToolOutput}``.

    With no matching hooks this yields nothing.
    """
    if is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
        return

    hook_event = hook_input["hook_event_name"]
    hook_name = f"{hook_event}:{match_query}" if match_query else hook_event


    matching_hooks = get_matching_hooks(hook_event, hook_input)
    if not matching_hooks:
        return

    # JSON we pipe on stdin. Shared across the batch (hook_input is never mutated).
    try:
        json_input = json.dumps(hook_input)
    except (TypeError, ValueError) as error:
        log_error(Exception(f"Failed to stringify hook {hook_name} input: {error}"))
        json_input = "{}"

    # Progress message per hook before execution.
    for matched in matching_hooks:
        hook = matched["hook"]
        from tabvis.utils.messages import create_progress_message

        yield {
            "message": create_progress_message(
                tool_use_id=tool_use_id,
                parent_tool_use_id=tool_use_id,
                data={
                    "type": "hook_progress",
                    "hookEvent": hook_event,
                    "hookName": hook_name,
                    "command": get_hook_display_text(hook),
                },
            )
        }

    permission_behavior: str | None = None

    for matched in matching_hooks:
        hook = matched["hook"]
        # Only command hooks are implemented.
        if hook.get("type") not in ("command", None):
            log_for_debugging(f"Skipping unported hook type: {hook.get('type')}")
            continue

        command = get_hook_display_text(hook)
        hook_timeout_ms = int(hook["timeout"] * 1000) if hook.get("timeout") else timeout_ms

        try:
            run = await exec_command_hook(
                hook, hook_event, hook_name, json_input, hook_timeout_ms
            )
        except Exception as error:  # noqa: BLE001 - mirror TS per-hook catch
            yield {
                "message": create_attachment_message(
                    {
                        "type": "hook_non_blocking_error",
                        "hookName": hook_name,
                        "toolUseID": tool_use_id,
                        "hookEvent": hook_event,
                        "stderr": f"Failed to run: {get_error_message(error)}",
                        "stdout": "",
                        "exitCode": 1,
                        "command": command,
                    }
                )
            }
            continue

        result = await _process_command_hook_result(
            run, hook, command, hook_name, tool_use_id, hook_event
        )

        # --- Aggregate / yield (mirrors the executeHooks result loop) ---
        if result.get("preventContinuation"):
            yield {"preventContinuation": True, "stopReason": result.get("stopReason")}

        if result.get("blockingError"):
            yield {"blockingError": result["blockingError"]}

        if result.get("message"):
            yield {"message": result["message"]}

        if result.get("systemMessage"):
            yield {
                "message": create_attachment_message(
                    {
                        "type": "hook_system_message",
                        "content": result["systemMessage"],
                        "hookName": hook_name,
                        "toolUseID": tool_use_id,
                        "hookEvent": hook_event,
                    }
                )
            }

        if result.get("additionalContext"):
            yield {"additionalContexts": [result["additionalContext"]]}

        if result.get("updatedMCPToolOutput"):
            yield {"updatedMCPToolOutput": result["updatedMCPToolOutput"]}

        # Permission behavior precedence: deny > ask > allow.
        this_behavior = result.get("permissionBehavior")
        if this_behavior:
            if this_behavior == "deny":
                permission_behavior = "deny"
            elif this_behavior == "ask":
                if permission_behavior != "deny":
                    permission_behavior = "ask"
            elif this_behavior == "allow":
                if not permission_behavior:
                    permission_behavior = "allow"
            # passthrough: no change.

        if permission_behavior is not None:
            updated_input = (
                result.get("updatedInput")
                if this_behavior in ("allow", "ask")
                else None
            )
            yield {
                "permissionBehavior": permission_behavior,
                "hookPermissionDecisionReason": result.get("hookPermissionDecisionReason"),
                "hookSource": matched.get("hookSource"),
                "updatedInput": updated_input,
            }

        # Passthrough updatedInput (no permission decision from THIS hook).
        if result.get("updatedInput") and this_behavior is None:
            yield {"updatedInput": result["updatedInput"]}


async def _process_command_hook_result(
    run: dict[str, Any],
    hook: dict[str, Any],
    command: str,
    hook_name: str,
    tool_use_id: str,
    hook_event: str,
) -> dict[str, Any]:
    """Map an :func:`exec_command_hook` run dict to a partial HookResult dict.

    Mirrors the post-``execCommandHook`` branch of ``executeHooks``: aborted → ``hook_cancelled``;
    JSON stdout → :func:`process_hook_json_output`; exit 0 → ``hook_success``; exit 2 →
    ``blockingError``; other non-zero → ``hook_non_blocking_error``.
    """
    if run.get("aborted"):
        return {
            "message": create_attachment_message(
                {
                    "type": "hook_cancelled",
                    "hookName": hook_name,
                    "toolUseID": tool_use_id,
                    "hookEvent": hook_event,
                    "command": command,
                }
            )
        }

    parsed = parse_hook_output(run["stdout"])
    if "validationError" in parsed:
        return {
            "message": create_attachment_message(
                {
                    "type": "hook_non_blocking_error",
                    "hookName": hook_name,
                    "toolUseID": tool_use_id,
                    "hookEvent": hook_event,
                    "stderr": f"JSON validation failed: {parsed['validationError']}",
                    "stdout": run["stdout"],
                    "exitCode": 1,
                    "command": command,
                }
            )
        }

    json_obj = parsed.get("json")
    if json_obj is not None:
        if is_async_hook_json_output(json_obj):
            return {}  # async responses are backgrounded (deferred); treat as success/no-op.
        return process_hook_json_output(
            json_obj,
            command=command,
            hook_name=hook_name,
            tool_use_id=tool_use_id,
            hook_event=hook_event,
            expected_hook_event=hook_event,
            stdout=run["stdout"],
            stderr=run["stderr"],
            exit_code=run["status"],
        )

    status = run["status"]
    if status == 0:
        return {
            "message": create_attachment_message(
                {
                    "type": "hook_success",
                    "hookName": hook_name,
                    "toolUseID": tool_use_id,
                    "hookEvent": hook_event,
                    "content": run["stdout"].strip(),
                    "stdout": run["stdout"],
                    "stderr": run["stderr"],
                    "exitCode": status,
                    "command": command,
                }
            )
        }

    if status == 2:
        return {
            "blockingError": {
                "blockingError": (
                    f"[{hook.get('command')}]: {run['stderr'] or 'No stderr output'}"
                ),
                "command": hook.get("command"),
            }
        }

    return {
        "message": create_attachment_message(
            {
                "type": "hook_non_blocking_error",
                "hookName": hook_name,
                "toolUseID": tool_use_id,
                "hookEvent": hook_event,
                "stderr": (
                    "Failed with non-blocking status code: "
                    f"{run['stderr'].strip() or 'No stderr output'}"
                ),
                "stdout": run["stdout"],
                "exitCode": status,
                "command": command,
            }
        )
    }


# ----------------------------------------------------------------------------------------------
# PreToolUse and PostToolUse event entrypoints
# ----------------------------------------------------------------------------------------------


async def execute_pre_tool_hooks(
    tool_name: str,
    tool_use_id: str,
    tool_input: Any,
    tool_use_context: Any,
    permission_mode: str | None = None,
    timeout_ms: int = TOOL_HOOK_EXECUTION_TIMEOUT_MS,
) -> AsyncGenerator[dict[str, Any], None]:
    """Build the PreToolUse hook input and run the matching hooks.

    The ``hasHookForEvent`` fast-path and ``requestPrompt``/``toolInputSummary`` wiring are not
    implemented in this build.
    """
    agent_info = _agent_info_from_context(tool_use_context)
    hook_input = {
        **create_base_hook_input(permission_mode, None, agent_info),
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }
    async for result in execute_hooks(
        hook_input=hook_input,
        tool_use_id=tool_use_id,
        match_query=tool_name,
        timeout_ms=timeout_ms,
    ):
        yield result


async def execute_post_tool_hooks(
    tool_name: str,
    tool_use_id: str,
    tool_input: Any,
    tool_response: Any,
    tool_use_context: Any,
    permission_mode: str | None = None,
    timeout_ms: int = TOOL_HOOK_EXECUTION_TIMEOUT_MS,
) -> AsyncGenerator[dict[str, Any], None]:
    """Build the PostToolUse hook input and run the matching hooks."""
    agent_info = _agent_info_from_context(tool_use_context)
    hook_input = {
        **create_base_hook_input(permission_mode, None, agent_info),
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
        "tool_use_id": tool_use_id,
    }
    async for result in execute_hooks(
        hook_input=hook_input,
        tool_use_id=tool_use_id,
        match_query=tool_name,
        timeout_ms=timeout_ms,
    ):
        yield result


def _agent_info_from_context(context: Any) -> dict[str, str]:
    """Extract ``{agentId, agentType}`` from a ToolUseContext (used by createBaseHookInput)."""
    info: dict[str, str] = {}
    agent_id = getattr(context, "agent_id", None)
    agent_type = getattr(context, "agent_type", None)
    if agent_id:
        info["agentId"] = agent_id
    if agent_type:
        info["agentType"] = agent_type
    return info
