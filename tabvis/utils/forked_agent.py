"""Utilities for running and inspecting forked agents.

Helpers for running forked agent query loops with usage tracking. Forked agents:
1. Share identical cache-critical params with the parent to guarantee prompt-cache hits
2. Track full usage metrics across the entire query loop
3. Log metrics via the ``tengu_fork_agent_query`` event when complete
4. Isolate mutable state to prevent interference with the main agent loop
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tabvis.agent.query import QueryParams, Terminal, query  # type: ignore[attr-defined]
from tabvis.agent.api.empty_usage import EMPTY_USAGE
from tabvis.agent.api.model_client import accumulate_usage, update_usage
from tabvis.tool import QueryChainTracking, ToolUseContext
from tabvis.utils.abort_controller import create_child_abort_controller
from tabvis.utils.crypto import random_uuid
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.file_state_cache import clone_file_state_cache
from tabvis.utils.messages import create_user_message
from tabvis.utils.permissions.permission_setup import parse_tool_list_from_cli
from tabvis.utils.session_storage import record_sidechain_transcript
from tabvis.utils.tool_result_storage import clone_content_replacement_state
from tabvis.utils.uuid import create_agent_id

__all__ = [
    "CacheSafeParams",
    "ForkedAgentParams",
    "ForkedAgentResult",
    "PreparedForkedContext",
    "SubagentContextOverrides",
    "create_cache_safe_params",
    "create_get_app_state_with_allowed_tools",
    "create_subagent_context",
    "extract_result_text",
    "get_last_cache_safe_params",
    "prepare_forked_command_context",
    "run_forked_agent",
    "save_cache_safe_params",
]


@dataclass
class CacheSafeParams:
    """Parameters that must be identical between the fork and parent to share the prompt cache."""

    system_prompt: list[str]
    user_context: dict[str, str]
    system_context: dict[str, str]
    tool_use_context: ToolUseContext
    fork_context_messages: list[dict[str, Any]]


# Slot written by handle_stop_hooks after each turn so post-turn forks can share the main loop's
# prompt cache without each caller threading params through.
_last_cache_safe_params: CacheSafeParams | None = None


def save_cache_safe_params(params: CacheSafeParams | None) -> None:
    global _last_cache_safe_params
    _last_cache_safe_params = params


def get_last_cache_safe_params() -> CacheSafeParams | None:
    return _last_cache_safe_params


@dataclass
class SubagentContextOverrides:
    """Options for creating a subagent context. By default all mutable state is isolated."""

    options: Any = None
    agent_id: str | None = None
    agent_type: str | None = None
    messages: list[dict[str, Any]] | None = None
    read_file_state: Any = None
    abort_controller: Any = None
    get_app_state: Callable[[], Any] | None = None
    share_set_app_state: bool = False
    share_set_response_length: bool = False
    share_abort_controller: bool = False
    critical_system_reminder_experimental: str | None = None
    require_can_use_tool: bool | None = None
    content_replacement_state: Any = None


@dataclass
class ForkedAgentParams:
    prompt_messages: list[dict[str, Any]]
    cache_safe_params: CacheSafeParams
    can_use_tool: Any
    query_source: str
    fork_label: str
    overrides: SubagentContextOverrides | None = None
    max_output_tokens: int | None = None
    max_turns: int | None = None
    on_message: Callable[[dict[str, Any]], None] | None = None
    skip_transcript: bool | None = None
    skip_cache_write: bool | None = None


@dataclass
class ForkedAgentResult:
    messages: list[dict[str, Any]]
    total_usage: dict[str, Any]


def create_cache_safe_params(context: dict[str, Any]) -> CacheSafeParams:
    """Create :class:`CacheSafeParams` from a ``REPLHookContext`` (post-sampling hook fork)."""
    return CacheSafeParams(
        system_prompt=context["systemPrompt"],
        user_context=context["userContext"],
        system_context=context["systemContext"],
        tool_use_context=context["toolUseContext"],
        fork_context_messages=context["messages"],
    )


def create_get_app_state_with_allowed_tools(
    base_get_app_state: Callable[[], Any],
    allowed_tools: list[str],
) -> Callable[[], Any]:
    """Wrap ``get_app_state`` to add allowed tools to the permission context.

    Used by forked skill/command execution to grant tool permissions.
    """
    if len(allowed_tools) == 0:
        return base_get_app_state

    def _wrapped() -> Any:
        app_state = base_get_app_state()
        tpc = dict(app_state.get("toolPermissionContext") or {})
        always_allow = dict(tpc.get("alwaysAllowRules") or {})
        existing_command = always_allow.get("command") or []
        # ``[...new Set([...existing, ...allowed])]`` — de-dupe preserving first-seen order.
        merged: list[str] = []
        for item in [*existing_command, *allowed_tools]:
            if item not in merged:
                merged.append(item)
        always_allow["command"] = merged
        tpc["alwaysAllowRules"] = always_allow
        return {**app_state, "toolPermissionContext": tpc}

    return _wrapped


@dataclass
class PreparedForkedContext:
    skill_content: str
    modified_get_app_state: Callable[[], Any]
    base_agent: Any
    prompt_messages: list[dict[str, Any]]


async def prepare_forked_command_context(
    command: Any,
    args: str,
    context: ToolUseContext,
) -> PreparedForkedContext:
    """Prepare the context for executing a forked command/skill (shared by SkillTool + commands)."""
    skill_prompt = await command.get_prompt_for_command(args, context)
    skill_content = "\n".join(
        block["text"] if block.get("type") == "text" else "" for block in skill_prompt
    )

    allowed_tools = parse_tool_list_from_cli(getattr(command, "allowed_tools", None) or [])

    modified_get_app_state = create_get_app_state_with_allowed_tools(
        context.get_app_state, allowed_tools
    )

    agent_type_name = getattr(command, "agent", None) or "general-purpose"
    agents = context.options.agent_definitions["activeAgents"]
    base_agent = (
        next((a for a in agents if a.agent_type == agent_type_name), None)
        or next((a for a in agents if a.agent_type == "general-purpose"), None)
        or (agents[0] if agents else None)
    )

    if not base_agent:
        raise RuntimeError("No agent available for forked execution")

    prompt_messages = [create_user_message(content=skill_content)]

    return PreparedForkedContext(
        skill_content=skill_content,
        modified_get_app_state=modified_get_app_state,
        base_agent=base_agent,
        prompt_messages=prompt_messages,
    )


def _extract_text_content(blocks: list[dict[str, Any]], separator: str = "") -> str:
    """Flat text-block join."""
    return separator.join(b["text"] for b in blocks if b.get("type") == "text")


def _get_last_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Last ``type == 'assistant'``."""
    for msg in reversed(messages):
        if msg.get("type") == "assistant":
            return msg
    return None


def extract_result_text(
    agent_messages: list[dict[str, Any]],
    default_text: str = "Execution completed",
) -> str:
    """Extract result text from agent messages."""
    last_assistant_message = _get_last_assistant_message(agent_messages)
    if not last_assistant_message:
        return default_text

    text_content = _extract_text_content(
        (last_assistant_message.get("message") or {}).get("content") or [], "\n"
    )
    return text_content or default_text


def create_subagent_context(
    parent_context: ToolUseContext,
    overrides: SubagentContextOverrides | None = None,
) -> ToolUseContext:
    """Create an isolated :class:`ToolUseContext` for subagents.

    By default ALL mutable state is isolated: ``read_file_state`` cloned, a new child
    AbortController linked to the parent, ``get_app_state`` wrapped to set
    ``shouldAvoidPermissionPrompts``, mutation callbacks no-op'd, fresh collections, new ``agent_id``
    and a new ``query_tracking`` chain with incremented depth.
    """
    ov = overrides or SubagentContextOverrides()

    # abortController: explicit override > share parent's > new child.
    if ov.abort_controller is not None:
        abort_controller = ov.abort_controller
    elif ov.share_abort_controller:
        abort_controller = parent_context.abort_controller
    else:
        abort_controller = create_child_abort_controller(parent_context.abort_controller)

    if ov.get_app_state is not None:
        get_app_state = ov.get_app_state
    elif ov.share_abort_controller:
        get_app_state = parent_context.get_app_state
    else:

        def get_app_state() -> Any:  # noqa: F811
            state = parent_context.get_app_state()
            tpc = (state or {}).get("toolPermissionContext") or {}
            if tpc.get("shouldAvoidPermissionPrompts"):
                return state
            return {
                **state,
                "toolPermissionContext": {**tpc, "shouldAvoidPermissionPrompts": True},
            }

    parent_depth = parent_context.query_tracking.depth if parent_context.query_tracking else -1

    content_replacement_state = (
        ov.content_replacement_state
        if ov.content_replacement_state is not None
        else (
            clone_content_replacement_state(parent_context.content_replacement_state)
            if parent_context.content_replacement_state
            else None
        )
    )

    return ToolUseContext(
        options=ov.options if ov.options is not None else parent_context.options,
        abort_controller=abort_controller,
        read_file_state=clone_file_state_cache(
            ov.read_file_state if ov.read_file_state is not None else parent_context.read_file_state
        ),
        get_app_state=get_app_state,
        set_app_state=(
            parent_context.set_app_state if ov.share_set_app_state else (lambda _f: None)
        ),
        messages=ov.messages if ov.messages is not None else parent_context.messages,
        # Task registration/kill must always reach the root store, even when set_app_state is a
        # no-op — otherwise async agents' background bash tasks are never registered/killed.
        set_app_state_for_tasks=(
            parent_context.set_app_state_for_tasks or parent_context.set_app_state
        ),
        set_in_progress_tool_use_ids=lambda _f: None,
        set_response_length=(
            parent_context.set_response_length
            if ov.share_set_response_length
            else (lambda _f: None)
        ),
        update_file_history_state=lambda _f: None,
        # Attribution is scoped/functional — safe to share even when set_app_state is stubbed.
        update_attribution_state=parent_context.update_attribution_state,
        # UI callbacks — None for subagents (can't control parent UI).
        add_notification=None,
        agent_id=ov.agent_id if ov.agent_id is not None else create_agent_id(),
        agent_type=ov.agent_type,
        query_tracking=QueryChainTracking(chain_id=random_uuid(), depth=parent_depth + 1),
        file_reading_limits=parent_context.file_reading_limits,
        user_modified=parent_context.user_modified,
        require_can_use_tool=ov.require_can_use_tool,
        content_replacement_state=content_replacement_state,
    )


async def run_forked_agent(params: ForkedAgentParams) -> ForkedAgentResult:
    """Run a forked agent query loop and track cache-hit metrics.

    :class:`tabvis.agent.query.QueryParams` does not accept the cache-sharing params
    (``user_context`` / ``system_context`` / ``query_source`` / ``max_output_tokens_override``
    / ``skip_cache_write``) in this build; they are carried on the resolved
    :class:`CacheSafeParams`. The usage accumulation and transcript recording below record
    cache-hit metrics per query.
    """
    start_time = time.time() * 1000
    output_messages: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {**EMPTY_USAGE}

    csp = params.cache_safe_params

    isolated_tool_use_context = create_subagent_context(csp.tool_use_context, params.overrides)

    # Do NOT filter incomplete tool calls here — dangling tool_uses are repaired downstream.
    initial_messages: list[dict[str, Any]] = [*csp.fork_context_messages, *params.prompt_messages]

    agent_id = None if params.skip_transcript else create_agent_id(params.fork_label)
    last_recorded_uuid: str | None = None
    if agent_id:
        try:
            await record_sidechain_transcript(initial_messages, agent_id)
        except Exception as err:  # noqa: BLE001
            log_for_debugging(
                f"Forked agent [{params.fork_label}] failed to record initial transcript: {err}"
            )
        last_recorded_uuid = (
            initial_messages[-1]["uuid"] if len(initial_messages) > 0 else None
        )

    try:
        async for message in query(
            QueryParams(
                messages=initial_messages,
                system_prompt=csp.system_prompt,
                tools=csp.tool_use_context.options.tools,
                can_use_tool=params.can_use_tool,
                tool_use_context=isolated_tool_use_context,
                max_turns=params.max_turns,
            )
        ):
            mtype = _msg_type(message)
            if mtype == "stream_event":
                event = _get(message, "event")
                if event is not None and _get(event, "type") == "message_delta":
                    usage = _get(event, "usage")
                    if usage:
                        turn_usage = update_usage({**EMPTY_USAGE}, usage)
                        total_usage = accumulate_usage(total_usage, turn_usage)
                continue
            if mtype == "stream_request_start":
                continue
            if isinstance(message, Terminal):
                continue

            log_for_debugging(
                f"Forked agent [{params.fork_label}] received message: type={mtype}"
            )

            output_messages.append(message)
            if params.on_message:
                params.on_message(message)

            if agent_id and mtype in ("assistant", "user", "progress"):
                try:
                    await record_sidechain_transcript([message], agent_id, last_recorded_uuid)
                except Exception as err:  # noqa: BLE001
                    log_for_debugging(
                        f"Forked agent [{params.fork_label}] failed to record transcript: {err}"
                    )
                if mtype != "progress":
                    last_recorded_uuid = _get(message, "uuid")
    finally:
        # Release the cloned file-state cache + fork context messages.
        clear = getattr(isolated_tool_use_context.read_file_state, "clear", None)
        if callable(clear):
            clear()
        initial_messages.clear()

    log_for_debugging(
        f"Forked agent [{params.fork_label}] finished: {len(output_messages)} messages, "
        f"types=[{', '.join(_msg_type(m) or '' for m in output_messages)}], totalUsage: "
        f"input={total_usage['input_tokens']} output={total_usage['output_tokens']} "
        f"cacheRead={total_usage['cache_read_input_tokens']} "
        f"cacheCreate={total_usage['cache_creation_input_tokens']}"
    )

    duration_ms = int(time.time() * 1000 - start_time)

    _log_fork_agent_query_event(
        fork_label=params.fork_label,
        query_source=params.query_source,
        duration_ms=duration_ms,
        message_count=len(output_messages),
        total_usage=total_usage,
        query_tracking=csp.tool_use_context.query_tracking,
    )

    return ForkedAgentResult(messages=output_messages, total_usage=total_usage)


def _log_fork_agent_query_event(
    *,
    fork_label: str,
    query_source: str,
    duration_ms: int,
    message_count: int,
    total_usage: dict[str, Any],
    query_tracking: QueryChainTracking | None,
) -> None:
    """Log the ``tengu_fork_agent_query`` event with full NonNullableUsage fields."""
    total_input_tokens = (
        total_usage["input_tokens"]
        + total_usage["cache_creation_input_tokens"]
        + total_usage["cache_read_input_tokens"]
    )
    cache_hit_rate = (
        total_usage["cache_read_input_tokens"] / total_input_tokens
        if total_input_tokens > 0
        else 0
    )

    cache_creation = total_usage.get("cache_creation") or {}
    metadata: dict[str, Any] = {
        "forkLabel": fork_label,
        "querySource": query_source,
        "durationMs": duration_ms,
        "messageCount": message_count,
        "inputTokens": total_usage["input_tokens"],
        "outputTokens": total_usage["output_tokens"],
        "cacheReadInputTokens": total_usage["cache_read_input_tokens"],
        "cacheCreationInputTokens": total_usage["cache_creation_input_tokens"],
        "serviceTier": total_usage.get("service_tier"),
        "cacheCreationEphemeral1hTokens": cache_creation.get("ephemeral_1h_input_tokens"),
        "cacheCreationEphemeral5mTokens": cache_creation.get("ephemeral_5m_input_tokens"),
        "cacheHitRate": cache_hit_rate,
    }
    if query_tracking:
        metadata["queryChainId"] = query_tracking.chain_id
        metadata["queryDepth"] = query_tracking.depth


def _msg_type(message: Any) -> str | None:
    if isinstance(message, Terminal):
        return "terminal"
    return _get(message, "type")


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
