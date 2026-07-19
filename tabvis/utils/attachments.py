"""Attachment expansion

The big one. Computes the per-turn ``<system-reminder>`` attachments that get injected into
the conversation: ``@file`` / ``@dir`` / ``@agent`` / ``@server:uri`` at-mentions, image pastes,
plan-mode reminders, todo/task reminders, IDE selections, diagnostics, skill listings, teammate
mailbox, and many gated deltas.

CYCLE NOTE — this module sits in a *mutually-recursive cycle* with ``tokens``, ``tool_search``,
``mcp_instructions_delta``, ``context_analysis``, ``analyze_context``, ``session_start``,
``conversation_recovery``, and ``services/compact/{compact,auto_compact,post_compact_cleanup,
micro_compact}``, plus the not-yet-implemented ``messages`` text helpers. **Every** cross-cycle (and
not-yet-implemented) reference is broken with ``if TYPE_CHECKING:`` (type-only) + a FUNCTION-LOCAL
(lazy) import at the call site, so this module imports standalone even before its siblings exist
on disk. Do NOT add a top-level import of any cycle sibling.

Casing: Python identifiers snake_case; the ``Attachment`` payload dicts round-trip into the
transcript so they keep their wire keys verbatim (``displayPath``/``source_uuid``/``imagePasteIds``
/``addedTypes``/``isInitial`` etc.). ``async function*`` → ``async def`` + ``yield``.
``fs/promises`` → ``asyncio.to_thread`` over stdlib.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tabvis.utils.array import uniq
from tabvis.utils.string_utils import count_char_in_string

if TYPE_CHECKING:  # type-only; never imported at runtime (cycle-safe)
    from tabvis.tool import ToolUseContext
    from tabvis.types.message import AttachmentMessage, Message

# Attachment is a tagged-union of plain dicts (kept open so wire keys round-trip verbatim).
Attachment = dict[str, Any]

# --------------------------------------------------------------------------------------------
# Constants (UPPER_CASE; faithful to the TS `as const` objects).
# --------------------------------------------------------------------------------------------

TODO_REMINDER_CONFIG = {
    "TURNS_SINCE_WRITE": 10,
    "TURNS_BETWEEN_REMINDERS": 10,
}

PLAN_MODE_ATTACHMENT_CONFIG = {
    "TURNS_BETWEEN_ATTACHMENTS": 5,
    "FULL_REMINDER_EVERY_N_ATTACHMENTS": 5,
}

VERIFY_PLAN_REMINDER_CONFIG = {
    "TURNS_BETWEEN_REMINDERS": 10,
}

INLINE_NOTIFICATION_MODES = {"prompt", "task-notification"}

# When skill-search is enabled and the filtered (bundled + MCP) listing exceeds this count,
# fall back to bundled-only.
FILTERED_LISTING_MAX = 30


# --------------------------------------------------------------------------------------------
# Tiny local helpers (the TS imports `errors.toError`/`errors.isAbortError`/`debug.logAntError`
# rewire to the shared helpers when they land).
# --------------------------------------------------------------------------------------------


def _to_error(value: object) -> Exception:
    return value if isinstance(value, BaseException) else Exception(str(value))  # type: ignore[return-value]


def _is_abort_error(value: object) -> bool:
    if isinstance(value, (asyncio.CancelledError,)):
        return True
    name = getattr(value, "name", None)
    return name in ("AbortError", "CanceledError") or "abort" in str(value).lower()


def _log_error(value: object) -> None:
    try:
        from tabvis.utils.log import log_error

        log_error(value)
    except Exception:
        pass


def _log_ant_error(message: str, value: object) -> None:
    # debug.logAntError isn't implemented under that name; fold into log_for_debugging.
    try:
        from tabvis.utils.debug import log_for_debugging

        log_for_debugging(message, value)
    except Exception:
        pass


def _get_cwd() -> str:
    try:
        from tabvis.utils.cwd import get_cwd

        return get_cwd()
    except Exception:
        return os.getcwd()


def _relative(target: str) -> str:
    """``path.relative(getCwd(), target)`` — display path relative to CWD."""
    try:
        return os.path.relpath(target, _get_cwd())
    except Exception:
        return target


def _is_env_truthy(env_var: str) -> bool:
    from tabvis.utils.env_utils import is_env_truthy

    return is_env_truthy(os.environ.get(env_var))


# --------------------------------------------------------------------------------------------
# Top-level orchestration: getAttachments / getAttachmentMessages.
# --------------------------------------------------------------------------------------------


async def get_attachments(
    input: str | None,
    tool_use_context: ToolUseContext,
    ide_selection: dict[str, Any] | None,
    queued_commands: list[dict[str, Any]],
    messages: list[Message] | None = None,
    query_source: str | None = None,
    options: dict[str, Any] | None = None,
) -> list[Attachment]:
    """Compute the per-turn attachment list. Faithful to the TS orchestration ordering."""
    if _is_env_truthy("TABVIS_DISABLE_ATTACHMENTS") or _is_env_truthy("TABVIS_SIMPLE"):
        # Bare/Coworker still depend on task-notification drains — return only those.
        return await get_queued_command_attachments(queued_commands)

    from tabvis.utils.abort_controller import create_abort_controller

    abort_controller = create_abort_controller()
    loop = asyncio.get_event_loop()
    timeout_handle = loop.call_later(1.0, abort_controller.abort)
    context = _with_abort(tool_use_context, abort_controller)

    is_main_thread = not getattr(tool_use_context, "agent_id", None)

    user_attachment_results: list[list[Attachment]] = []
    if input:
        active_agents = _active_agents(tool_use_context)
        user_attachment_results = await asyncio.gather(
            _maybe("at_mentioned_files", lambda: process_at_mentioned_files(input, context)),
            _maybe("mcp_resources", lambda: process_mcp_resource_attachments(input, context)),
            _maybe(
                "agent_mentions",
                lambda: _coro(process_agent_mentions(input, active_agents)),
            ),
        )

    options_obj = _ctx_options(tool_use_context)
    all_thread_coros: list[Any] = [
        _maybe("queued_commands", lambda: get_queued_command_attachments(queued_commands)),
        _maybe("date_change", lambda: _coro(get_date_change_attachments(messages))),
        _maybe("ultrathink_effort", lambda: _coro(get_ultrathink_effort_attachment(input))),
        _maybe(
            "deferred_tools_delta",
            lambda: _coro(
                get_deferred_tools_delta_attachment(
                    _opt(options_obj, "tools", []),
                    _opt(options_obj, "main_loop_model", ""),
                    messages,
                    {
                        "callSite": "attachments_main"
                        if is_main_thread
                        else "attachments_subagent",
                        "querySource": query_source,
                    },
                )
            ),
        ),
        _maybe(
            "agent_listing_delta",
            lambda: _coro(get_agent_listing_delta_attachment(tool_use_context, messages)),
        ),
        _maybe(
            "mcp_instructions_delta",
            lambda: _coro(
                get_mcp_instructions_delta_attachment(
                    _opt(options_obj, "mcp_clients", []),
                    _opt(options_obj, "tools", []),
                    _opt(options_obj, "main_loop_model", ""),
                    messages,
                )
            ),
        ),
        _maybe("changed_files", lambda: get_changed_files(context)),
        _maybe("dynamic_skill", lambda: get_dynamic_skill_attachments(context)),
        _maybe("skill_listing", lambda: get_skill_listing_attachments(context)),
        _maybe("plan_mode", lambda: get_plan_mode_attachments(messages, tool_use_context)),
        _maybe("plan_mode_exit", lambda: get_plan_mode_exit_attachment(tool_use_context)),
        _maybe("todo_reminders", lambda: _todo_or_task_reminders(messages, tool_use_context)),
    ]

    if _is_agent_swarms_enabled():
        if query_source != "session_memory":
            all_thread_coros.append(
                _maybe(
                    "teammate_mailbox",
                    lambda: get_teammate_mailbox_attachments(tool_use_context),
                )
            )
        all_thread_coros.append(
            _maybe("team_context", lambda: _coro(get_team_context_attachment(messages or [])))
        )

    all_thread_coros.append(
        _maybe(
            "critical_system_reminder",
            lambda: _coro(get_critical_system_reminder_attachment(tool_use_context)),
        )
    )

    main_thread_coros: list[Any] = []
    if is_main_thread:
        main_thread_coros = [
            _maybe(
                "ide_selection",
                lambda: get_selected_lines_from_ide(ide_selection, tool_use_context),
            ),
            _maybe(
                "ide_opened_file",
                lambda: get_opened_file_from_ide(ide_selection, tool_use_context),
            ),
            _maybe("output_style", lambda: _coro(get_output_style_attachment())),
            _maybe("diagnostics", lambda: get_diagnostic_attachments(tool_use_context)),
            _maybe("unified_tasks", lambda: get_unified_task_attachments(tool_use_context)),
            _maybe("async_hook_responses", lambda: get_async_hook_response_attachments()),
            _maybe(
                "token_usage",
                lambda: _coro(
                    get_token_usage_attachment(
                        messages or [], _opt(options_obj, "main_loop_model", "")
                    )
                ),
            ),
            _maybe(
                "budget_usd",
                lambda: _coro(
                    get_max_budget_usd_attachment(_opt(options_obj, "max_budget_usd", None))
                ),
            ),
            _maybe("output_token_usage", lambda: _coro(get_output_token_usage_attachment())),
            _maybe(
                "verify_plan_reminder",
                lambda: get_verify_plan_reminder_attachment(messages, tool_use_context),
            ),
        ]

    thread_results, main_results = await asyncio.gather(
        asyncio.gather(*all_thread_coros) if all_thread_coros else _coro([]),
        asyncio.gather(*main_thread_coros) if main_thread_coros else _coro([]),
    )

    timeout_handle.cancel()

    out: list[Attachment] = []
    for group in (user_attachment_results, thread_results, main_results):
        for sub in group:
            if sub:
                for a in sub:
                    if a is not None:
                        out.append(a)
    return out


def _todo_or_task_reminders(
    messages: list[Message] | None, tool_use_context: ToolUseContext
) -> Any:
    from tabvis.utils.tasks import is_todo_v2_enabled

    if is_todo_v2_enabled():
        return get_task_reminder_attachments(messages, tool_use_context)
    return get_todo_reminder_attachments(messages, tool_use_context)


async def _coro(value: Any) -> Any:
    """Wrap an eager value in an awaitable (TS ``Promise.resolve``)."""
    return value


def _with_abort(tool_use_context: ToolUseContext, abort_controller: Any) -> ToolUseContext:
    """``{ ...toolUseContext, abortController }`` shallow override."""
    try:
        import copy

        ctx = copy.copy(tool_use_context)
        ctx.abort_controller = abort_controller
        return ctx
    except Exception:
        return tool_use_context


def _ctx_options(tool_use_context: ToolUseContext) -> Any:
    return getattr(tool_use_context, "options", None)


def _opt(options_obj: Any, name: str, default: Any) -> Any:
    if options_obj is None:
        return default
    return getattr(options_obj, name, default)


def _active_agents(tool_use_context: ToolUseContext) -> list[Any]:
    options_obj = _ctx_options(tool_use_context)
    agent_defs = _opt(options_obj, "agent_definitions", None)
    if agent_defs is None:
        return []
    return getattr(agent_defs, "active_agents", None) or agent_defs.get("activeAgents", []) if isinstance(agent_defs, dict) else getattr(agent_defs, "active_agents", [])


def _is_agent_swarms_enabled() -> bool:
    try:
        from tabvis.utils.agent_swarms_enabled import is_agent_swarms_enabled

        return is_agent_swarms_enabled()
    except Exception:
        return False


async def _maybe(label: str, f: Callable[[], Any]) -> list[Attachment]:
    """The TS ``maybe`` wrapper: run ``f``, swallow errors (→ ``[]``), 5%-sample telemetry."""
    start = _now_ms()
    try:
        result = await f()
        if result is None:
            result = []
        duration = _now_ms() - start
        if random.random() < 0.05:
            from tabvis.utils.slow_operations import json_stringify

            size = 0
            for a in result:
                if a is not None:
                    s = json_stringify(a)
                    size += len(s or "")
        return result
    except Exception as e:  # noqa: BLE001 (faithful: maybe swallows everything → [])
        duration = _now_ms() - start
        if random.random() < 0.05:
            pass
        _log_error(e)
        _log_ant_error(f"Attachment error in {label}", e)
        return []


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# --------------------------------------------------------------------------------------------
# Queued commands / pending agent messages / image blocks.
# --------------------------------------------------------------------------------------------


async def get_queued_command_attachments(
    queued_commands: list[dict[str, Any]] | None,
) -> list[Attachment]:
    if not queued_commands:
        return []
    filtered = [c for c in queued_commands if c.get("mode") in INLINE_NOTIFICATION_MODES]

    async def build(c: dict[str, Any]) -> Attachment:
        image_blocks = await build_image_content_blocks(c.get("pastedContents"))
        prompt: Any = c.get("value")
        if image_blocks:
            value = c.get("value")
            if isinstance(value, str):
                text_value = value
            else:
                from tabvis.utils.messages import extract_text_content

                text_value = extract_text_content(value, "\n")
            prompt = [{"type": "text", "text": text_value}, *image_blocks]
        from tabvis.types.text_input_types import get_image_paste_ids

        return {
            "type": "queued_command",
            "prompt": prompt,
            "source_uuid": c.get("uuid"),
            "imagePasteIds": get_image_paste_ids(c.get("pastedContents")),
            "commandMode": c.get("mode"),
            "origin": c.get("origin"),
            "isMeta": c.get("isMeta"),
        }

    return list(await asyncio.gather(*[build(c) for c in filtered]))


async def build_image_content_blocks(
    pasted_contents: dict[int, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not pasted_contents:
        return []
    from tabvis.types.text_input_types import is_valid_image_paste

    image_contents = [c for c in pasted_contents.values() if is_valid_image_paste(c)]
    if not image_contents:
        return []
    from tabvis.utils.image_resizer import maybe_resize_and_downsample_image_block

    async def one(img: dict[str, Any]) -> dict[str, Any]:
        image_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": img.get("mediaType") or "image/png",
                "data": img.get("content"),
            },
        }
        resized = await maybe_resize_and_downsample_image_block(image_block)
        return resized["block"] if isinstance(resized, dict) else resized.block

    return list(await asyncio.gather(*[one(i) for i in image_contents]))


# --------------------------------------------------------------------------------------------
# Plan mode.
# --------------------------------------------------------------------------------------------


def get_plan_mode_attachment_turn_count(messages: list[Message]) -> dict[str, Any]:
    turns_since = 0
    found = False
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not message:
            continue
        if (
            message.get("type") == "user"
            and not message.get("isMeta")
            and not _has_tool_result_content(_msg_content(message))
        ):
            turns_since += 1
        elif message.get("type") == "attachment" and (
            _att_type(message) in ("plan_mode", "plan_mode_reentry")
        ):
            found = True
            break
    return {"turnCount": turns_since, "foundPlanModeAttachment": found}


def count_plan_mode_attachments_since_last_exit(messages: list[Message]) -> int:
    count = 0
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message and message.get("type") == "attachment":
            t = _att_type(message)
            if t == "plan_mode_exit":
                break
            if t == "plan_mode":
                count += 1
    return count


async def get_plan_mode_attachments(
    messages: list[Message] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    app_state = tool_use_context.get_app_state()
    permission_context = _perm_ctx(app_state)
    if _perm_mode(permission_context) != "plan":
        return []

    if messages and len(messages) > 0:
        counts = get_plan_mode_attachment_turn_count(messages)
        if (
            counts["foundPlanModeAttachment"]
            and counts["turnCount"] < PLAN_MODE_ATTACHMENT_CONFIG["TURNS_BETWEEN_ATTACHMENTS"]
        ):
            return []

    from tabvis.bootstrap.state import (
        has_exited_plan_mode_in_session,
        set_has_exited_plan_mode)
    from tabvis.utils.plans import get_plan, get_plan_file_path

    agent_id = getattr(tool_use_context, "agent_id", None)
    plan_file_path = get_plan_file_path(agent_id)
    existing_plan = get_plan(agent_id)

    attachments: list[Attachment] = []
    if has_exited_plan_mode_in_session() and existing_plan is not None:
        attachments.append({"type": "plan_mode_reentry", "planFilePath": plan_file_path})
        set_has_exited_plan_mode(False)

    attachment_count = count_plan_mode_attachments_since_last_exit(messages or []) + 1
    reminder_type = (
        "full"
        if attachment_count % PLAN_MODE_ATTACHMENT_CONFIG["FULL_REMINDER_EVERY_N_ATTACHMENTS"] == 1
        else "sparse"
    )
    attachments.append(
        {
            "type": "plan_mode",
            "reminderType": reminder_type,
            "isSubAgent": bool(agent_id),
            "planFilePath": plan_file_path,
            "planExists": existing_plan is not None,
        }
    )
    return attachments


async def get_plan_mode_exit_attachment(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    from tabvis.bootstrap.state import (
        needs_plan_mode_exit_attachment,
        set_needs_plan_mode_exit_attachment)

    if not needs_plan_mode_exit_attachment():
        return []

    app_state = tool_use_context.get_app_state()
    if _perm_mode(_perm_ctx(app_state)) == "plan":
        set_needs_plan_mode_exit_attachment(False)
        return []

    set_needs_plan_mode_exit_attachment(False)

    from tabvis.utils.plans import get_plan, get_plan_file_path

    agent_id = getattr(tool_use_context, "agent_id", None)
    plan_file_path = get_plan_file_path(agent_id)
    plan_exists = get_plan(agent_id) is not None
    return [{"type": "plan_mode_exit", "planFilePath": plan_file_path, "planExists": plan_exists}]


# --------------------------------------------------------------------------------------------
# Date change / ultrathink / deferred-tools / agent-listing / mcp-instructions deltas.
# --------------------------------------------------------------------------------------------


def get_date_change_attachments(messages: list[Message] | None) -> list[Attachment]:
    from tabvis.bootstrap.state import get_last_emitted_date, set_last_emitted_date
    from tabvis.constants.common import get_local_iso_date

    current_date = get_local_iso_date()
    last_date = get_last_emitted_date()

    if last_date is None:
        set_last_emitted_date(current_date)
        return []
    if current_date == last_date:
        return []
    set_last_emitted_date(current_date)
    return [{"type": "date_change", "newDate": current_date}]


def get_ultrathink_effort_attachment(input: str | None) -> list[Attachment]:
    from tabvis.utils.thinking import has_ultrathink_keyword, is_ultrathink_enabled

    if not is_ultrathink_enabled() or not input or not has_ultrathink_keyword(input):
        return []
    return [{"type": "ultrathink_effort", "level": "high"}]


def get_deferred_tools_delta_attachment(
    tools: list[Any],
    model: str,
    messages: list[Message] | None,
    scan_context: dict[str, Any] | None = None,
) -> list[Attachment]:
    # tool_search is a cycle sibling — lazy import so this module loads standalone.
    from tabvis.utils.tool_search import (
        get_deferred_tools_delta,
        is_deferred_tools_delta_enabled,
        is_tool_search_enabled_optimistic,
        is_tool_search_tool_available,
        model_supports_tool_reference)

    if not is_deferred_tools_delta_enabled():
        return []
    if not is_tool_search_enabled_optimistic():
        return []
    if not model_supports_tool_reference(model):
        return []
    if not is_tool_search_tool_available(tools):
        return []
    delta = get_deferred_tools_delta(tools, messages or [], scan_context)
    if not delta:
        return []
    return [{"type": "deferred_tools_delta", **delta}]


def get_agent_listing_delta_attachment(
    tool_use_context: ToolUseContext, messages: list[Message] | None
) -> list[Attachment]:
    from tabvis.tool import tool_matches_name
    from tabvis.agent.tools.agent_tool import AGENT_TOOL_NAME

    # AgentTool/prompt + loadAgentsDir helpers aren't implemented yet — lazy + graceful fallback.
    try:
        from tabvis.agent.tools.agent_tool import (  # type: ignore[attr-defined]
            format_agent_line,
            should_inject_agent_list_in_messages)
    except Exception:
        return []

    if not should_inject_agent_list_in_messages():
        return []

    options_obj = _ctx_options(tool_use_context)
    tools = _opt(options_obj, "tools", [])
    if not any(tool_matches_name(t, AGENT_TOOL_NAME) for t in tools):
        return []

    agent_defs = _opt(options_obj, "agent_definitions", None)
    active_agents = getattr(agent_defs, "active_agents", []) if agent_defs else []
    allowed_agent_types = getattr(agent_defs, "allowed_agent_types", None) if agent_defs else None

    from tabvis.agent.mcp.mcp_string_utils import mcp_info_from_string

    mcp_servers: set[str] = set()
    for tool in tools:
        info = mcp_info_from_string(getattr(tool, "name", ""))
        if info:
            mcp_servers.add(info["serverName"] if isinstance(info, dict) else info.server_name)

    permission_context = _perm_ctx(tool_use_context.get_app_state())

    try:
        from tabvis.agent.tools.agent_defs import filter_agents_by_mcp_requirements  # type: ignore
        from tabvis.utils.permissions.permissions import filter_denied_agents  # type: ignore

        filtered = filter_denied_agents(
            filter_agents_by_mcp_requirements(active_agents, list(mcp_servers)),
            permission_context,
            AGENT_TOOL_NAME,
        )
    except Exception:
        return []

    if allowed_agent_types:
        filtered = [a for a in filtered if _agent_type(a) in allowed_agent_types]

    announced: set[str] = set()
    for msg in messages or []:
        if msg.get("type") != "attachment":
            continue
        att = _attachment(msg)
        if not att or att.get("type") != "agent_listing_delta":
            continue
        for t in att.get("addedTypes", []):
            announced.add(t)
        for t in att.get("removedTypes", []):
            announced.discard(t)

    current_types = {_agent_type(a) for a in filtered}
    added = [a for a in filtered if _agent_type(a) not in announced]
    removed = [t for t in announced if t not in current_types]

    if not added and not removed:
        return []

    added.sort(key=lambda a: _agent_type(a))
    removed.sort()

    return [
        {
            "type": "agent_listing_delta",
            "addedTypes": [_agent_type(a) for a in added],
            "addedLines": [format_agent_line(a) for a in added],
            "removedTypes": removed,
            "isInitial": len(announced) == 0,
            "showConcurrencyNote": True,
        }
    ]


def get_mcp_instructions_delta_attachment(
    mcp_clients: list[Any],
    tools: list[Any],
    model: str,
    messages: list[Message] | None,
) -> list[Attachment]:
    from tabvis.utils.mcp_instructions_delta import (
        get_mcp_instructions_delta,
        is_mcp_instructions_delta_enabled)

    if not is_mcp_instructions_delta_enabled():
        return []
    delta = get_mcp_instructions_delta(mcp_clients, messages or [], [])
    if not delta:
        return []
    return [{"type": "mcp_instructions_delta", **delta}]


def get_critical_system_reminder_attachment(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    reminder = getattr(tool_use_context, "critical_system_reminder_experimental", None)
    if not reminder:
        return []
    return [{"type": "critical_system_reminder", "content": reminder}]


def get_output_style_attachment() -> list[Attachment]:
    try:
        from tabvis.utils.settings.settings import get_initial_settings

        settings = get_initial_settings()
        output_style = (settings.output_style if settings else None) or "default"
    except Exception:
        output_style = "default"
    if output_style == "default":
        return []
    return [{"type": "output_style", "style": output_style}]


# --------------------------------------------------------------------------------------------
# IDE selection / opened file.
# --------------------------------------------------------------------------------------------


async def get_selected_lines_from_ide(
    ide_selection: dict[str, Any] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    from tabvis.utils.ide import get_connected_ide_name

    options_obj = _ctx_options(tool_use_context)
    ide_name = get_connected_ide_name(_opt(options_obj, "mcp_clients", []))
    if (
        not ide_name
        or not ide_selection
        or ide_selection.get("lineStart") is None
        or not ide_selection.get("text")
        or not ide_selection.get("filePath")
    ):
        return []

    app_state = tool_use_context.get_app_state()
    if is_file_read_denied(ide_selection["filePath"], _perm_ctx(app_state)):
        return []

    line_start = ide_selection["lineStart"]
    line_count = ide_selection.get("lineCount", 1)
    return [
        {
            "type": "selected_lines_in_ide",
            "ideName": ide_name,
            "lineStart": line_start,
            "lineEnd": line_start + line_count - 1,
            "filename": ide_selection["filePath"],
            "content": ide_selection["text"],
            "displayPath": _relative(ide_selection["filePath"]),
        }
    ]


async def get_opened_file_from_ide(
    ide_selection: dict[str, Any] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    if not ide_selection or not ide_selection.get("filePath") or ide_selection.get("text"):
        return []

    app_state = tool_use_context.get_app_state()
    if is_file_read_denied(ide_selection["filePath"], _perm_ctx(app_state)):
        return []

    return [{"type": "opened_file_in_ide", "filename": ide_selection["filePath"]}]


# --------------------------------------------------------------------------------------------
# At-mention processing (@file / @dir / @agent / @server:uri).
# --------------------------------------------------------------------------------------------


async def process_at_mentioned_files(
    input: str, tool_use_context: ToolUseContext
) -> list[Attachment]:
    files = extract_at_mentioned_files(input)
    if not files:
        return []

    from tabvis.utils.path import expand_path

    app_state = tool_use_context.get_app_state()

    async def one(file: str) -> Attachment | None:
        try:
            parsed = parse_at_mentioned_file_lines(file)
            filename = parsed["filename"]
            line_start = parsed.get("lineStart")
            line_end = parsed.get("lineEnd")
            absolute_filename = expand_path(filename)

            if is_file_read_denied(absolute_filename, _perm_ctx(app_state)):
                return None

            try:
                stats = await asyncio.to_thread(os.stat, absolute_filename)
                if _is_dir(stats):
                    try:
                        entries = await asyncio.to_thread(_scandir_names, absolute_filename)
                        max_dir_entries = 1000
                        truncated = len(entries) > max_dir_entries
                        names = entries[:max_dir_entries]
                        if truncated:
                            names.append(
                                f"… and {len(entries) - max_dir_entries} more entries"
                            )
                        stdout = "\n".join(names)
                        return {
                            "type": "directory",
                            "path": absolute_filename,
                            "content": stdout,
                            "displayPath": _relative(absolute_filename),
                        }
                    except Exception:
                        return None
            except Exception:
                pass

            return await generate_file_attachment(
                absolute_filename,
                tool_use_context,
                "tengu_at_mention_extracting_filename_success",
                "tengu_at_mention_extracting_filename_error",
                "at-mention",
                {
                    "offset": line_start,
                    "limit": (line_end - line_start + 1)
                    if (line_end and line_start)
                    else None,
                },
            )
        except Exception:
            return None

    results = await asyncio.gather(*[one(f) for f in files])
    return [r for r in results if r]


def process_agent_mentions(input: str, agents: list[Any]) -> list[Attachment]:
    agent_mentions = extract_agent_mentions(input)
    if not agent_mentions:
        return []

    results: list[Attachment] = []
    for mention in agent_mentions:
        # TS: mention.replace('agent-', '') — JS String.replace replaces the FIRST occurrence.
        agent_type = mention.replace("agent-", "", 1)
        agent_def = next((d for d in agents if _agent_type(d) == agent_type), None)
        if not agent_def:
            continue
        results.append({"type": "agent_mention", "agentType": _agent_type(agent_def)})
    return results


async def process_mcp_resource_attachments(
    input: str, tool_use_context: ToolUseContext
) -> list[Attachment]:
    resource_mentions = extract_mcp_resource_mentions(input)
    if not resource_mentions:
        return []

    options_obj = _ctx_options(tool_use_context)
    mcp_clients = _opt(options_obj, "mcp_clients", []) or []
    mcp_resources = _opt(options_obj, "mcp_resources", {}) or {}

    async def one(mention: str) -> Attachment | None:
        try:
            parts = mention.split(":")
            server_name = parts[0] if parts else ""
            uri = ":".join(parts[1:])
            if not server_name or not uri:
                return None

            client = next((c for c in mcp_clients if getattr(c, "name", None) == server_name), None)
            if not client or getattr(client, "type", None) != "connected":
                return None

            server_resources = mcp_resources.get(server_name, [])
            resource_info = next((r for r in server_resources if r.get("uri") == uri), None)
            if not resource_info:
                return None

            try:
                result = await client.client.read_resource({"uri": uri})
                return {
                    "type": "mcp_resource",
                    "server": server_name,
                    "uri": uri,
                    "name": resource_info.get("name") or uri,
                    "description": resource_info.get("description"),
                    "content": result,
                }
            except Exception as error:  # noqa: BLE001
                _log_error(error)
                return None
        except Exception:
            return None

    results = await asyncio.gather(*[one(m) for m in resource_mentions])
    return [r for r in results if r is not None]


def extract_at_mentioned_files(content: str) -> list[str]:
    """Extract ``@file``/``@"quoted path"`` mentions (line ranges + fragments kept on the path)."""
    import re

    quoted_re = re.compile(r'(^|\s)@"([^"]+)"')
    regular_re = re.compile(r"(^|\s)@([^\s]+)\b")

    quoted_matches: list[str] = []
    regular_matches: list[str] = []

    for m in quoted_re.finditer(content):
        if m.group(2) and not m.group(2).endswith(" (agent)"):
            quoted_matches.append(m.group(2))

    for m in regular_re.finditer(content):
        whole = m.group(0)
        filename = whole[whole.index("@") + 1 :]
        if not filename.startswith('"'):
            regular_matches.append(filename)

    return uniq([*quoted_matches, *regular_matches])


def extract_mcp_resource_mentions(content: str) -> list[str]:
    """Extract ``@server:uri`` MCP resource mentions."""
    import re

    at_mention_re = re.compile(r"(^|\s)@([^\s]+:[^\s]+)\b")
    matches = [m.group(0) for m in at_mention_re.finditer(content)]
    return uniq([m[m.index("@") + 1 :] for m in matches])


def extract_agent_mentions(content: str) -> list[str]:
    """Extract ``@agent-<type>`` and ``@"<type> (agent)"`` mentions."""
    import re

    results: list[str] = []

    quoted_agent_re = re.compile(r'(^|\s)@"([\w:.@-]+) \(agent\)"')
    for m in quoted_agent_re.finditer(content):
        if m.group(2):
            results.append(m.group(2))

    unquoted_agent_re = re.compile(r"(^|\s)@(agent-[\w:.@-]+)")
    for m in unquoted_agent_re.finditer(content):
        whole = m.group(0)
        results.append(whole[whole.index("@") + 1 :])

    return uniq(results)


def parse_at_mentioned_file_lines(mention: str) -> dict[str, Any]:
    """Parse ``file.txt#L10-20`` / ``file.txt#heading`` / ``file.txt`` → filename + line range."""
    import re

    match = re.match(r"^([^#]+)(?:#L(\d+)(?:-(\d+))?)?(?:#[^#]*)?$", mention)
    if not match:
        return {"filename": mention}

    filename = match.group(1)
    line_start_str = match.group(2)
    line_end_str = match.group(3)
    line_start = int(line_start_str, 10) if line_start_str else None
    line_end = int(line_end_str, 10) if line_end_str else line_start
    return {"filename": filename or mention, "lineStart": line_start, "lineEnd": line_end}


# --------------------------------------------------------------------------------------------
# Changed files / relevant memories / dynamic skills.
# --------------------------------------------------------------------------------------------


async def get_changed_files(tool_use_context: ToolUseContext) -> list[Attachment]:
    from tabvis.utils.file_state_cache import cache_keys

    file_paths = cache_keys(tool_use_context.read_file_state)
    if not file_paths:
        return []

    from tabvis.utils.path import expand_path

    app_state = tool_use_context.get_app_state()

    async def one(file_path: str) -> Attachment | None:
        file_state = tool_use_context.read_file_state.get(file_path)
        if not file_state:
            return None
        if file_state.get("offset") is not None or file_state.get("limit") is not None:
            return None

        normalized_path = expand_path(file_path)
        if is_file_read_denied(normalized_path, _perm_ctx(app_state)):
            return None

        try:
            from tabvis.utils.file import get_file_modification_time_async

            mtime = await get_file_modification_time_async(normalized_path)
            if mtime <= file_state.get("timestamp", 0):
                return None

            from tabvis.agent.tools.file_read_tool import FileReadTool

            file_input = {"file_path": normalized_path}
            is_valid = await FileReadTool.validate_input(file_input, tool_use_context)
            if not is_valid.get("result") if isinstance(is_valid, dict) else not getattr(
                is_valid, "result", False
            ):
                return None

            result = await FileReadTool.call(file_input, tool_use_context)
            data = result.data if hasattr(result, "data") else result.get("data")
            if data.get("type") == "text":
                from tabvis.agent.tools.file_edit_tool import get_snippet_for_two_file_diff  # type: ignore

                snippet = get_snippet_for_two_file_diff(
                    file_state.get("content"), data["file"]["content"]
                )
                if snippet == "":
                    return None
                return {
                    "type": "edited_text_file",
                    "filename": normalized_path,
                    "snippet": snippet,
                }

            if data.get("type") == "image":
                try:
                    from tabvis.agent.tools.file_read_tool import (  # type: ignore[attr-defined]
                        read_image_with_token_budget)

                    image_data = await read_image_with_token_budget(normalized_path)
                    return {
                        "type": "edited_image_file",
                        "filename": normalized_path,
                        "content": image_data,
                    }
                except Exception as compression_error:  # noqa: BLE001
                    _log_error(compression_error)
                    return None
            return None
        except Exception as err:  # noqa: BLE001
            from tabvis.utils.errors import is_enoent

            if is_enoent(err):
                tool_use_context.read_file_state.delete(file_path)
            return None

    results = await asyncio.gather(*[one(fp) for fp in file_paths])
    return [r for r in results if r is not None]


async def get_dynamic_skill_attachments(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    attachments: list[Attachment] = []
    triggers = getattr(tool_use_context, "dynamic_skill_dir_triggers", None)
    if triggers and len(triggers) > 0:

        async def per_dir(skill_dir: str) -> dict[str, Any]:
            try:
                entries = await asyncio.to_thread(_scandir_dir_entries, skill_dir)
                candidates = [name for name, is_dir_or_link in entries if is_dir_or_link]

                async def check(name: str) -> str | None:
                    try:
                        await asyncio.to_thread(
                            os.stat, os.path.join(skill_dir, name, "SKILL.md")
                        )
                        return name
                    except Exception:
                        return None

                checked = await asyncio.gather(*[check(n) for n in candidates])
                return {"skillDir": skill_dir, "skillNames": [n for n in checked if n]}
            except Exception:
                return {"skillDir": skill_dir, "skillNames": []}

        per_dir_results = await asyncio.gather(*[per_dir(d) for d in list(triggers)])
        for r in per_dir_results:
            if r["skillNames"]:
                attachments.append(
                    {
                        "type": "dynamic_skill",
                        "skillDir": r["skillDir"],
                        "skillNames": r["skillNames"],
                        "displayPath": _relative(r["skillDir"]),
                    }
                )
        triggers.clear()
    return attachments


# Skill-listing dedup state — keyed by agentId (empty string = main thread).
_sent_skill_names: dict[str, set[str]] = {}
_suppress_next = False


def reset_sent_skill_names() -> None:
    global _suppress_next
    _sent_skill_names.clear()
    _suppress_next = False


def suppress_next_skill_listing() -> None:
    global _suppress_next
    _suppress_next = True


def filter_to_bundled_and_mcp(commands: list[Any]) -> list[Any]:
    filtered = [c for c in commands if _loaded_from(c) in ("bundled", "mcp")]
    if len(filtered) > FILTERED_LISTING_MAX:
        return [c for c in filtered if _loaded_from(c) == "bundled"]
    return filtered


async def get_skill_listing_attachments(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    global _suppress_next
    if os.environ.get("NODE_ENV") == "test":
        return []

    from tabvis.tool import tool_matches_name
    from tabvis.agent.tools.skill_tool import SKILL_TOOL_NAME

    options_obj = _ctx_options(tool_use_context)
    tools = _opt(options_obj, "tools", [])
    if not any(tool_matches_name(t, SKILL_TOOL_NAME) for t in tools):
        return []

    from tabvis.bootstrap.state import get_project_root

    cwd = get_project_root()
    try:
        from tabvis.ui.commands import get_mcp_skill_commands, get_skill_tool_commands  # type: ignore
    except Exception:
        return []

    local_commands = await get_skill_tool_commands(cwd)
    mcp_skills = get_mcp_skill_commands(tool_use_context.get_app_state().mcp.commands)

    if mcp_skills:
        from tabvis.utils.array import uniq as _uniq  # noqa: F401

        seen_names: set[str] = set()
        all_commands = []
        for cmd in [*local_commands, *mcp_skills]:
            name = _cmd_name(cmd)
            if name not in seen_names:
                seen_names.add(name)
                all_commands.append(cmd)
    else:
        all_commands = local_commands

    agent_key = getattr(tool_use_context, "agent_id", None) or ""
    sent = _sent_skill_names.get(agent_key)
    if sent is None:
        sent = set()
        _sent_skill_names[agent_key] = sent

    if _suppress_next:
        _suppress_next = False
        for cmd in all_commands:
            sent.add(_cmd_name(cmd))
        return []

    new_skills = [c for c in all_commands if _cmd_name(c) not in sent]
    if not new_skills:
        return []

    is_initial = len(sent) == 0
    for cmd in new_skills:
        sent.add(_cmd_name(cmd))

    from tabvis.bootstrap.state import get_sdk_betas
    from tabvis.agent.tools.skill_tool import format_commands_within_budget  # type: ignore
    from tabvis.utils.context import get_context_window_for_model
    from tabvis.utils.debug import log_for_debugging

    log_for_debugging(
        f"Sending {len(new_skills)} skills via attachment "
        f"({'initial' if is_initial else 'dynamic'}, {len(sent)} total sent)"
    )
    context_window_tokens = get_context_window_for_model(
        _opt(options_obj, "main_loop_model", ""), get_sdk_betas()
    )
    content = format_commands_within_budget(new_skills, context_window_tokens)
    return [
        {
            "type": "skill_listing",
            "content": content,
            "skillCount": len(new_skills),
            "isInitial": is_initial,
        }
    ]


# --------------------------------------------------------------------------------------------
# Diagnostics.
# --------------------------------------------------------------------------------------------


async def get_diagnostic_attachments(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    from tabvis.tool import tool_matches_name
    from tabvis.agent.tools.bash_tool import BASH_TOOL_NAME

    options_obj = _ctx_options(tool_use_context)
    if not any(tool_matches_name(t, BASH_TOOL_NAME) for t in _opt(options_obj, "tools", [])):
        return []

    from tabvis.services.diagnostic_tracking import diagnostic_tracker

    new_diagnostics = await diagnostic_tracker.get_new_diagnostics()
    if not new_diagnostics:
        return []
    return [{"type": "diagnostics", "files": new_diagnostics, "isNew": True}]


# --------------------------------------------------------------------------------------------
# getAttachmentMessages (async generator) + createAttachmentMessage.
# --------------------------------------------------------------------------------------------


async def get_attachment_messages(
    input: str | None,
    tool_use_context: ToolUseContext,
    ide_selection: dict[str, Any] | None,
    queued_commands: list[dict[str, Any]],
    messages: list[Message] | None = None,
    query_source: str | None = None,
    options: dict[str, Any] | None = None,
):
    attachments = await get_attachments(
        input, tool_use_context, ide_selection, queued_commands, messages, query_source, options
    )
    if not attachments:
        return
    for attachment in attachments:
        yield create_attachment_message(attachment)


def create_attachment_message(attachment: Attachment) -> AttachmentMessage:
    from tabvis.utils.crypto import random_uuid

    return {
        "attachment": attachment,
        "type": "attachment",
        "uuid": random_uuid(),
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


# --------------------------------------------------------------------------------------------
# File-attachment generation (PDF reference / already-read / truncated read).
# --------------------------------------------------------------------------------------------


async def try_get_pdf_reference(filename: str) -> Attachment | None:
    from tabvis.utils.fs_operations import get_fs_implementation
    from tabvis.utils.pdf import get_pdf_page_count
    from tabvis.utils.pdf_utils import is_pdf_extension

    ext = os.path.splitext(filename)[1].lower()
    if not is_pdf_extension(ext):
        return None
    try:
        from tabvis.constants.api_limits import PDF_AT_MENTION_INLINE_THRESHOLD

        fs = get_fs_implementation()
        stats, page_count = await asyncio.gather(
            _maybe_await(fs.stat(filename)), get_pdf_page_count(filename)
        )
        size = getattr(stats, "size", None)
        if size is None and isinstance(stats, dict):
            size = stats.get("size")
        import math

        effective_page_count = (
            page_count if page_count is not None else math.ceil(size / (100 * 1024))
        )
        if effective_page_count > PDF_AT_MENTION_INLINE_THRESHOLD:
            return {
                "type": "pdf_reference",
                "filename": filename,
                "pageCount": effective_page_count,
                "fileSize": size,
                "displayPath": _relative(filename),
            }
    except Exception:
        pass
    return None


async def generate_file_attachment(
    filename: str,
    tool_use_context: ToolUseContext,
    success_event_name: str,
    error_event_name: str,
    mode: str,
    options: dict[str, Any] | None = None,
) -> Attachment | None:
    options = options or {}
    offset = options.get("offset")
    limit = options.get("limit")

    app_state = tool_use_context.get_app_state()
    if is_file_read_denied(filename, _perm_ctx(app_state)):
        return None

    from tabvis.agent.tools.file_read_tool import (
        FILE_READ_TOOL_NAME,  # noqa: F401
        MAX_LINES_TO_READ,
        FileReadTool)

    if mode == "at-mention":
        try:
            from tabvis.agent.tools.file_read_tool import get_default_file_reading_limits  # type: ignore
            from tabvis.utils.file import is_file_within_read_size_limit  # type: ignore

            within = is_file_within_read_size_limit(
                filename, get_default_file_reading_limits()["maxSizeBytes"]
            )
        except Exception:
            within = True
        if not within:
            from tabvis.utils.pdf_utils import is_pdf_extension

            ext = os.path.splitext(filename)[1].lower()
            if not is_pdf_extension(ext):
                try:
                    from tabvis.utils.fs_operations import get_fs_implementation

                    stats = await _maybe_await(get_fs_implementation().stat(filename))
                    size = getattr(stats, "size", None) or (
                        stats.get("size") if isinstance(stats, dict) else None
                    )
                    return None
                except Exception:
                    pass

    if mode == "at-mention":
        pdf_ref = await try_get_pdf_reference(filename)
        if pdf_ref:
            return pdf_ref

    existing_file_state = tool_use_context.read_file_state.get(filename)
    if existing_file_state and mode == "at-mention":
        try:
            from tabvis.utils.file import get_file_modification_time_async

            mtime_ms = await get_file_modification_time_async(filename)
            ts = existing_file_state.get("timestamp")
            if ts is not None and ts <= mtime_ms and mtime_ms == ts:
                content = existing_file_state.get("content", "")
                num_lines = count_char_in_string(content, "\n") + 1
                return {
                    "type": "already_read_file",
                    "filename": filename,
                    "displayPath": _relative(filename),
                    "content": {
                        "type": "text",
                        "file": {
                            "filePath": filename,
                            "content": content,
                            "numLines": num_lines,
                            "startLine": offset or 1,
                            "totalLines": num_lines,
                        },
                    },
                }
        except Exception:
            pass

    try:
        file_input = {"file_path": filename, "offset": offset, "limit": limit}

        async def read_truncated_file() -> Attachment | None:
            if mode == "compact":
                return {
                    "type": "compact_file_reference",
                    "filename": filename,
                    "displayPath": _relative(filename),
                }
            local_app_state = tool_use_context.get_app_state()
            if is_file_read_denied(filename, _perm_ctx(local_app_state)):
                return None
            try:
                truncated_input = {
                    "file_path": filename,
                    "offset": offset or 1,
                    "limit": MAX_LINES_TO_READ,
                }
                result = await FileReadTool.call(truncated_input, tool_use_context)
                return {
                    "type": "file",
                    "filename": filename,
                    "content": _result_data(result),
                    "truncated": True,
                    "displayPath": _relative(filename),
                }
            except Exception:
                return None

        is_valid = await FileReadTool.validate_input(file_input, tool_use_context)
        valid = is_valid.get("result") if isinstance(is_valid, dict) else getattr(
            is_valid, "result", False
        )
        if not valid:
            return None

        try:
            result = await FileReadTool.call(file_input, tool_use_context)
            return {
                "type": "file",
                "filename": filename,
                "content": _result_data(result),
                "displayPath": _relative(filename),
            }
        except Exception as error:  # noqa: BLE001
            from tabvis.agent.tools.file_read_tool import MaxFileReadTokenExceededError
            from tabvis.utils.read_file_in_range import FileTooLargeError

            if isinstance(error, (MaxFileReadTokenExceededError, FileTooLargeError)):
                return await read_truncated_file()
            raise
    except Exception:
        return None


# --------------------------------------------------------------------------------------------
# Todo / Task reminders.
# --------------------------------------------------------------------------------------------


def get_todo_reminder_turn_counts(messages: list[Message]) -> dict[str, int]:
    last_todo_write_index = -1
    last_reminder_index = -1
    assistant_turns_since_write = 0
    assistant_turns_since_reminder = 0

    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not message:
            continue
        if message.get("type") == "assistant":
            if _is_thinking_message(message):
                continue
            if (
                last_todo_write_index == -1
                and "message" in message
                and isinstance((message.get("message") or {}).get("content"), list)
                and any(
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") == "TodoWrite"
                    for b in message["message"]["content"]
                )
            ):
                last_todo_write_index = i
            if last_todo_write_index == -1:
                assistant_turns_since_write += 1
            if last_reminder_index == -1:
                assistant_turns_since_reminder += 1
        elif (
            last_reminder_index == -1
            and message.get("type") == "attachment"
            and _att_type(message) == "todo_reminder"
        ):
            last_reminder_index = i
        if last_todo_write_index != -1 and last_reminder_index != -1:
            break

    return {
        "turnsSinceLastTodoWrite": assistant_turns_since_write,
        "turnsSinceLastReminder": assistant_turns_since_reminder,
    }


async def get_todo_reminder_attachments(
    messages: list[Message] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    from tabvis.tool import tool_matches_name
    from tabvis.agent.tools.todo_write_tool import TODO_WRITE_TOOL_NAME

    options_obj = _ctx_options(tool_use_context)
    if not any(
        tool_matches_name(t, TODO_WRITE_TOOL_NAME) for t in _opt(options_obj, "tools", [])
    ):
        return []
    if not messages:
        return []

    counts = get_todo_reminder_turn_counts(messages)
    if (
        counts["turnsSinceLastTodoWrite"] >= TODO_REMINDER_CONFIG["TURNS_SINCE_WRITE"]
        and counts["turnsSinceLastReminder"] >= TODO_REMINDER_CONFIG["TURNS_BETWEEN_REMINDERS"]
    ):
        from tabvis.bootstrap.state import get_session_id

        todo_key = getattr(tool_use_context, "agent_id", None) or get_session_id()
        app_state = tool_use_context.get_app_state()
        todos = (app_state.todos or {}).get(todo_key, []) if hasattr(app_state, "todos") else []
        return [{"type": "todo_reminder", "content": todos, "itemCount": len(todos)}]
    return []


def get_task_reminder_turn_counts(messages: list[Message]) -> dict[str, int]:
    from tabvis.constants.tools import TASK_CREATE_TOOL_NAME
    from tabvis.constants.tools import TASK_UPDATE_TOOL_NAME

    last_task_index = -1
    last_reminder_index = -1
    assistant_turns_since_task = 0
    assistant_turns_since_reminder = 0

    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not message:
            continue
        if message.get("type") == "assistant":
            if _is_thinking_message(message):
                continue
            if (
                last_task_index == -1
                and "message" in message
                and isinstance((message.get("message") or {}).get("content"), list)
                and any(
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") in (TASK_CREATE_TOOL_NAME, TASK_UPDATE_TOOL_NAME)
                    for b in message["message"]["content"]
                )
            ):
                last_task_index = i
            if last_task_index == -1:
                assistant_turns_since_task += 1
            if last_reminder_index == -1:
                assistant_turns_since_reminder += 1
        elif (
            last_reminder_index == -1
            and message.get("type") == "attachment"
            and _att_type(message) == "task_reminder"
        ):
            last_reminder_index = i
        if last_task_index != -1 and last_reminder_index != -1:
            break

    return {
        "turnsSinceLastTaskManagement": assistant_turns_since_task,
        "turnsSinceLastReminder": assistant_turns_since_reminder,
    }


async def get_task_reminder_attachments(
    messages: list[Message] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    from tabvis.utils.tasks import is_todo_v2_enabled

    if not is_todo_v2_enabled():
        return []

    from tabvis.tool import tool_matches_name
    from tabvis.constants.tools import TASK_UPDATE_TOOL_NAME

    options_obj = _ctx_options(tool_use_context)
    if not any(
        tool_matches_name(t, TASK_UPDATE_TOOL_NAME) for t in _opt(options_obj, "tools", [])
    ):
        return []
    if not messages:
        return []

    counts = get_task_reminder_turn_counts(messages)
    if (
        counts["turnsSinceLastTaskManagement"] >= TODO_REMINDER_CONFIG["TURNS_SINCE_WRITE"]
        and counts["turnsSinceLastReminder"] >= TODO_REMINDER_CONFIG["TURNS_BETWEEN_REMINDERS"]
    ):
        from tabvis.utils.tasks import get_task_list_id, list_tasks

        tasks = await list_tasks(get_task_list_id())
        return [{"type": "task_reminder", "content": tasks, "itemCount": len(tasks)}]
    return []


async def get_unified_task_attachments(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    from tabvis.utils.task.disk_output import get_task_output_path
    from tabvis.utils.task.framework import (
        apply_task_offsets_and_evictions,
        generate_task_attachments)

    app_state = tool_use_context.get_app_state()
    result = await generate_task_attachments(app_state)
    attachments = result["attachments"] if isinstance(result, dict) else result.attachments
    updated_offsets = result["updatedTaskOffsets"] if isinstance(result, dict) else result.updated_task_offsets
    evicted_ids = result["evictedTaskIds"] if isinstance(result, dict) else result.evicted_task_ids

    apply_task_offsets_and_evictions(
        tool_use_context.set_app_state, updated_offsets, evicted_ids
    )

    return [
        {
            "type": "task_status",
            "taskId": ta.get("taskId") if isinstance(ta, dict) else ta.task_id,
            "taskType": ta.get("taskType") if isinstance(ta, dict) else ta.task_type,
            "status": ta.get("status") if isinstance(ta, dict) else ta.status,
            "description": ta.get("description") if isinstance(ta, dict) else ta.description,
            "deltaSummary": ta.get("deltaSummary") if isinstance(ta, dict) else ta.delta_summary,
            "outputFilePath": get_task_output_path(
                ta.get("taskId") if isinstance(ta, dict) else ta.task_id
            ),
        }
        for ta in attachments
    ]


# --------------------------------------------------------------------------------------------
# Async hooks / teammate mailbox / team context.
# --------------------------------------------------------------------------------------------


async def get_async_hook_response_attachments() -> list[Attachment]:
    from tabvis.utils.hooks.async_hook_registry import (
        check_for_async_hook_responses,
        remove_delivered_async_hooks)

    responses = await check_for_async_hook_responses()
    if not responses:
        return []

    attachments: list[Attachment] = []
    for r in responses:
        attachments.append(
            {
                "type": "async_hook_response",
                "processId": _field(r, "process_id", "processId"),
                "hookName": _field(r, "hook_name", "hookName"),
                "hookEvent": _field(r, "hook_event", "hookEvent"),
                "toolName": _field(r, "tool_name", "toolName"),
                "response": _field(r, "response", "response"),
                "stdout": _field(r, "stdout", "stdout"),
                "stderr": _field(r, "stderr", "stderr"),
                "exitCode": _field(r, "exit_code", "exitCode"),
            }
        )

    process_ids = [_field(r, "process_id", "processId") for r in responses]
    remove_delivered_async_hooks(process_ids)
    return attachments


async def get_teammate_mailbox_attachments(
    tool_use_context: ToolUseContext,
) -> list[Attachment]:
    if not _is_agent_swarms_enabled():
        return []
    return []


def _remove_teammate_from_app_state(tool_use_context: ToolUseContext, teammate_id: str) -> None:
    def updater(prev: Any) -> Any:
        team_context = getattr(prev, "team_context", None) or (
            prev.get("teamContext") if isinstance(prev, dict) else None
        )
        if not team_context:
            return prev
        teammates = (
            team_context.get("teammates") if isinstance(team_context, dict) else None
        ) or {}
        if teammate_id not in teammates:
            return prev
        remaining = {k: v for k, v in teammates.items() if k != teammate_id}
        if isinstance(prev, dict):
            new_tc = {**team_context, "teammates": remaining}
            return {**prev, "teamContext": new_tc}
        return prev

    try:
        tool_use_context.set_app_state(updater)
    except Exception:
        pass


def _mark_inbox_processed(tool_use_context: ToolUseContext, pending_ids: set[Any]) -> None:
    def updater(prev: Any) -> Any:
        inbox = getattr(prev, "inbox", None) or (prev.get("inbox") if isinstance(prev, dict) else None)
        if not inbox:
            return prev
        messages = (inbox.get("messages") if isinstance(inbox, dict) else getattr(inbox, "messages", [])) or []
        new_messages = [
            {**m, "status": "processed"} if m.get("id") in pending_ids else m for m in messages
        ]
        if isinstance(prev, dict):
            return {**prev, "inbox": {"messages": new_messages}}
        return prev

    try:
        tool_use_context.set_app_state(updater)
    except Exception:
        pass


def get_team_context_attachment(messages: list[Message]) -> list[Attachment]:
    from tabvis.utils.env_utils import get_tabvis_config_home_dir
    from tabvis.utils.teammate import get_agent_id, get_agent_name, get_team_name

    team_name = get_team_name()
    agent_id = get_agent_id()
    agent_name = get_agent_name()

    if not team_name or not agent_id:
        return []

    if any(m.get("type") == "assistant" for m in messages):
        return []

    config_dir = get_tabvis_config_home_dir()
    return [
        {
            "type": "team_context",
            "agentId": agent_id,
            "agentName": agent_name or agent_id,
            "teamName": team_name,
            "teamConfigPath": f"{config_dir}/teams/{team_name}/config.json",
            "taskListPath": f"{config_dir}/tasks/{team_name}/",
        }
    ]


# --------------------------------------------------------------------------------------------
# Token usage / budget / output-token / verify-plan / compaction / context-efficiency.
# --------------------------------------------------------------------------------------------


def get_token_usage_attachment(messages: list[Message], model: str) -> list[Attachment]:
    if not _is_env_truthy("TABVIS_ENABLE_TOKEN_USAGE_ATTACHMENT"):
        return []
    # tokens + compact/auto_compact are cycle siblings — lazy.
    from tabvis.agent.compact.auto_compact import get_effective_context_window_size
    from tabvis.utils.tokens import token_count_from_last_api_response

    context_window = get_effective_context_window_size(model)
    used_tokens = token_count_from_last_api_response(messages)
    return [
        {
            "type": "token_usage",
            "used": used_tokens,
            "total": context_window,
            "remaining": context_window - used_tokens,
        }
    ]


def get_output_token_usage_attachment() -> list[Attachment]:
    # The TS gate is `if (false)` → always [].
    return []


def get_max_budget_usd_attachment(max_budget_usd: float | None = None) -> list[Attachment]:
    if max_budget_usd is None:
        return []
    from tabvis.bootstrap.state import get_total_cost_usd

    used_cost = get_total_cost_usd()
    return [
        {
            "type": "budget_usd",
            "used": used_cost,
            "total": max_budget_usd,
            "remaining": max_budget_usd - used_cost,
        }
    ]


def get_verify_plan_reminder_turn_count(messages: list[Message]) -> int:
    from tabvis.utils.message_predicates import is_human_turn

    turn_count = 0
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if message and is_human_turn(message):
            turn_count += 1
        if message and message.get("type") == "attachment" and _att_type(message) == "plan_mode_exit":
            return turn_count
    return 0


async def get_verify_plan_reminder_attachment(
    messages: list[Message] | None, tool_use_context: ToolUseContext
) -> list[Attachment]:
    return []


def get_compaction_reminder_attachment(messages: list[Message], model: str) -> list[Attachment]:
    return []


def get_context_efficiency_attachment(messages: list[Message]) -> list[Attachment]:
    # TS gate is `if (!false) return []` → always []. (snip nudge is dead-gated.)
    return []


# --------------------------------------------------------------------------------------------
# Permission gate.
# --------------------------------------------------------------------------------------------


def is_file_read_denied(file_path: str, tool_permission_context: Any) -> bool:
    from tabvis.utils.permissions.filesystem import matching_rule_for_input

    deny_rule = matching_rule_for_input(file_path, tool_permission_context, "read", "deny")
    return deny_rule is not None


# --------------------------------------------------------------------------------------------
# Small accessors (tolerate both dict-shaped envelopes and pydantic-ish objects).
# --------------------------------------------------------------------------------------------


def _msg_content(message: dict[str, Any]) -> Any:
    msg = message.get("message")
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None) if msg is not None else None


def _attachment(message: dict[str, Any]) -> dict[str, Any]:
    att = message.get("attachment")
    return att if isinstance(att, dict) else {}


def _att_type(message: dict[str, Any]) -> str | None:
    return _attachment(message).get("type")


def _perm_ctx(app_state: Any) -> Any:
    if isinstance(app_state, dict):
        return app_state.get("toolPermissionContext")
    return getattr(app_state, "tool_permission_context", None) or getattr(
        app_state, "toolPermissionContext", None
    )


def _perm_mode(permission_context: Any) -> Any:
    if permission_context is None:
        return None
    if isinstance(permission_context, dict):
        return permission_context.get("mode")
    return getattr(permission_context, "mode", None)


def _agent_type(agent_def: Any) -> str:
    if isinstance(agent_def, dict):
        return agent_def.get("agentType") or agent_def.get("agent_type")
    return getattr(agent_def, "agent_type", None) or getattr(agent_def, "agentType", "")


def _loaded_from(command: Any) -> str | None:
    if isinstance(command, dict):
        return command.get("loadedFrom") or command.get("loaded_from")
    return getattr(command, "loaded_from", None) or getattr(command, "loadedFrom", None)


def _cmd_name(command: Any) -> str:
    if isinstance(command, dict):
        return command.get("name")
    return getattr(command, "name", "")


def _is_thinking_message(message: dict[str, Any]) -> bool:
    try:
        from tabvis.utils.messages import is_thinking_message

        return is_thinking_message(message)
    except Exception:
        return False


def _field(obj: Any, snake: str, camel: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(snake, obj.get(camel))
    return getattr(obj, snake, None) if hasattr(obj, snake) else getattr(obj, camel, None)


def _result_field(result: Any, snake: str, camel: str, default: Any) -> Any:
    if isinstance(result, dict):
        if snake in result:
            return result[snake]
        if camel in result:
            return result[camel]
        return default
    val = getattr(result, snake, None)
    if val is not None:
        return val
    val = getattr(result, camel, None)
    return val if val is not None else default


def _result_data(result: Any) -> Any:
    if hasattr(result, "data"):
        return result.data
    if isinstance(result, dict):
        return result.get("data")
    return None


def _is_dir(stats: Any) -> bool:
    import stat as _stat

    mode = getattr(stats, "st_mode", None)
    if mode is not None:
        return _stat.S_ISDIR(mode)
    if hasattr(stats, "is_directory"):
        return bool(stats.is_directory())
    return False


def _scandir_names(path: str) -> list[str]:
    with os.scandir(path) as it:
        return [e.name for e in it]


def _scandir_dir_entries(path: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    with os.scandir(path) as it:
        for e in it:
            out.append((e.name, e.is_dir(follow_symlinks=False) or e.is_symlink()))
    return out


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value
