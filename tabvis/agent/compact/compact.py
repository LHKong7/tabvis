"""Conversation compaction — summarize older messages, preserve recent history.

Cycle note: this is the hub of the mutually-recursive compact cycle. Every
cross-cycle reference is broken with a ``TYPE_CHECKING`` type-only import plus a
function-local (lazy) runtime import, so this module imports standalone even
before its siblings (``attachments``, ``context_analysis``/``analyze_context``,
``tokens``, ``token_estimation``, ``tool_search``, ``session_start``,
``conversation_recovery``, the other ``compact/*`` modules, and the cycle-sibling
``ToolSearchTool``/``FileReadTool``) exist on disk. The only module-level
imports are leaves / already-verified deps.

Transcript envelopes stay plain dicts with verbatim wire keys
(``parentUuid``/``uuid``/``compactMetadata``/``toolUseResult`` etc.).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tabvis.bootstrap.state import get_invoked_skills_for_agent, mark_post_compaction
from tabvis.agent.api.errors import (
    PROMPT_TOO_LONG_ERROR_MESSAGE,
    starts_with_api_error_prefix)
from tabvis.agent.api.with_retry import get_retry_delay
from tabvis.agent.compact.grouping import group_messages_by_api_round
from tabvis.agent.compact.prompt import (
    get_compact_prompt,
    get_compact_user_summary_message,
    get_partial_compact_prompt)
from tabvis.services.internal_logging import log_permission_context_for_ants
from tabvis.utils.context import COMPACT_MAX_OUTPUT_TOKENS
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.file_state_cache import cache_to_object
from tabvis.utils.log import log_error
from tabvis.utils.memory.types import MEMORY_TYPE_VALUES
from tabvis.utils.messages import create_user_message, normalize_messages_for_api
from tabvis.utils.path import expand_path
from tabvis.utils.plans import get_plan, get_plan_file_path
from tabvis.utils.session_activity import (
    is_session_activity_tracking_active,
    send_session_activity_signal)
from tabvis.utils.session_storage import (
    get_transcript_path,
    re_append_session_metadata)
from tabvis.utils.sleep import sleep
from tabvis.utils.slow_operations import json_stringify
from tabvis.utils.system_prompt_type import as_system_prompt

if TYPE_CHECKING:  # type-only — never imported at runtime
    from tabvis.tool import ToolUseContext
    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.ids import AgentId
    from tabvis.types.message import (
        AssistantMessage,
        AttachmentMessage,
        HookResultMessage,
        Message,
        UserMessage)

    # These envelope shapes are not separate TypedDicts in the message
    # module yet; they are plain dicts at runtime.
    SystemCompactBoundaryMessage = dict[str, Any]
    PartialCompactDirection = str


POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
# Skills can be large. Per-skill truncation beats dropping — instructions at the
# top of a skill file are usually the critical part.
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
MAX_COMPACT_STREAMING_RETRIES = 2


# --- lazy proxies for cycle siblings -------------------------------------------------------------


def _get_max_output_tokens_for_model(model: str) -> int:
    try:
        from tabvis.agent.api.model_client import (  # type: ignore[attr-defined]
            get_max_output_tokens_for_model)

        return get_max_output_tokens_for_model(model)
    except (ImportError, AttributeError):
        from tabvis.utils.context import get_model_max_output_tokens

        return get_model_max_output_tokens(model)["default"]


def _query_model_with_streaming(**kwargs: Any) -> Any:
    from tabvis.agent.api.model_client import query_model_with_streaming

    return query_model_with_streaming(**kwargs)


def _get_feature_value(feature: str, default: Any) -> Any:

    return default


def _get_prompt_too_long_token_gap(response: AssistantMessage) -> int | None:
    try:
        from tabvis.agent.api.errors import (  # type: ignore[attr-defined]
            get_prompt_too_long_token_gap)

        return get_prompt_too_long_token_gap(response)
    except (ImportError, AttributeError):
        return None


def _rough_token_count_estimation(content: str) -> int:
    from tabvis.services.token_estimation import rough_token_count_estimation

    return rough_token_count_estimation(content)


def _rough_token_count_estimation_for_messages(messages: list[Message]) -> int:
    from tabvis.services.token_estimation import rough_token_count_estimation_for_messages

    return rough_token_count_estimation_for_messages(messages)


def _token_count_with_estimation(messages: list[Message]) -> int:
    from tabvis.utils.tokens import token_count_with_estimation

    return token_count_with_estimation(messages)


def _token_count_from_last_api_response(messages: list[Message]) -> int:
    from tabvis.utils.tokens import token_count_from_last_api_response

    return token_count_from_last_api_response(messages)


def _get_token_usage(message: AssistantMessage) -> Any:
    from tabvis.utils.tokens import get_token_usage

    return get_token_usage(message)


def _analyze_context(messages: list[Message]) -> Any:
    from tabvis.utils.context_analysis import analyze_context

    return analyze_context(messages)


def _token_stats_to_statsig_metrics(stats: Any) -> dict[str, Any]:
    from tabvis.utils.context_analysis import token_stats_to_statsig_metrics

    return token_stats_to_statsig_metrics(stats)


def _extract_discovered_tool_names(messages: list[Message]) -> set[str]:
    from tabvis.utils.tool_search import extract_discovered_tool_names

    return extract_discovered_tool_names(messages)


async def _is_tool_search_enabled(*args: Any) -> bool:
    from tabvis.utils.tool_search import is_tool_search_enabled

    return await is_tool_search_enabled(*args)


def _create_attachment_message(att: Any) -> AttachmentMessage:
    from tabvis.utils.attachments import create_attachment_message

    return create_attachment_message(att)


async def _generate_file_attachment(*args: Any) -> Any:
    from tabvis.utils.attachments import generate_file_attachment

    return await generate_file_attachment(*args)


def _get_agent_listing_delta_attachment(*args: Any) -> list[Any]:
    from tabvis.utils.attachments import get_agent_listing_delta_attachment

    return get_agent_listing_delta_attachment(*args)


def _get_deferred_tools_delta_attachment(*args: Any, **kwargs: Any) -> list[Any]:
    from tabvis.utils.attachments import get_deferred_tools_delta_attachment

    return get_deferred_tools_delta_attachment(*args, **kwargs)


def _get_mcp_instructions_delta_attachment(*args: Any) -> list[Any]:
    from tabvis.utils.attachments import get_mcp_instructions_delta_attachment

    return get_mcp_instructions_delta_attachment(*args)


async def _process_session_start_hooks(*args: Any) -> list[HookResultMessage]:
    from tabvis.utils.session_start import process_session_start_hooks

    return await process_session_start_hooks(*args)


async def _execute_pre_compact_hooks(payload: dict[str, Any], signal: Any) -> Any:
    from tabvis.utils.hooks import execute_pre_compact_hooks

    return await execute_pre_compact_hooks(payload, signal)


async def _execute_post_compact_hooks(payload: dict[str, Any], signal: Any) -> Any:
    from tabvis.utils.hooks import execute_post_compact_hooks

    return await execute_post_compact_hooks(payload, signal)


# --- messages helpers (leaf module) --------------------------------------------------------------


def _create_compact_boundary_message(*args: Any) -> SystemCompactBoundaryMessage:
    from tabvis.utils.messages import create_compact_boundary_message  # type: ignore[attr-defined]

    return create_compact_boundary_message(*args)


def _get_assistant_message_text(message: AssistantMessage) -> str | None:
    from tabvis.utils.messages import get_assistant_message_text  # type: ignore[attr-defined]

    return get_assistant_message_text(message)


def _get_last_assistant_message(messages: list[Message]) -> AssistantMessage | None:
    from tabvis.utils.messages import get_last_assistant_message  # type: ignore[attr-defined]

    return get_last_assistant_message(messages)


def _get_messages_after_compact_boundary(messages: list[Message]) -> list[Message]:
    from tabvis.utils.messages import (
        get_messages_after_compact_boundary)

    return get_messages_after_compact_boundary(messages)


def _is_compact_boundary_message(message: Message) -> bool:
    from tabvis.utils.messages import is_compact_boundary_message  # type: ignore[attr-defined]

    return is_compact_boundary_message(message)


def _on_compact_progress(context: ToolUseContext, payload: dict[str, Any]) -> None:
    cb = getattr(context, "on_compact_progress", None)
    if cb is not None:
        cb(payload)


def _call_opt(context: ToolUseContext, attr: str, *args: Any) -> None:
    cb = getattr(context, attr, None)
    if cb is not None:
        cb(*args)


# --- image / attachment stripping ----------------------------------------------------------------


def strip_images_from_messages(messages: list[Message]) -> list[Message]:
    """Strip image/document blocks from user messages before compaction.

    Images are not needed for a summary and can push the compaction call itself
    past the prompt-too-long limit. Replaces image/document blocks with a text
    marker so the summary still notes that media was shared.
    """
    result: list[Message] = []
    for message in messages:
        if message.get("type") != "user":
            result.append(message)
            continue

        content = message.get("message", {}).get("content")
        if not isinstance(content, list):
            result.append(message)
            continue

        has_media_block = False
        new_content: list[Any] = []
        for block in content:
            btype = block.get("type")
            if btype == "image":
                has_media_block = True
                new_content.append({"type": "text", "text": "[image]"})
                continue
            if btype == "document":
                has_media_block = True
                new_content.append({"type": "text", "text": "[document]"})
                continue
            # Also strip images/documents nested inside tool_result content arrays.
            if btype == "tool_result" and isinstance(block.get("content"), list):
                tool_has_media = False
                new_tool_content: list[Any] = []
                for item in block["content"]:
                    itype = item.get("type")
                    if itype == "image":
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[image]"})
                    elif itype == "document":
                        tool_has_media = True
                        new_tool_content.append({"type": "text", "text": "[document]"})
                    else:
                        new_tool_content.append(item)
                if tool_has_media:
                    has_media_block = True
                    new_content.append({**block, "content": new_tool_content})
                    continue
            new_content.append(block)

        if not has_media_block:
            result.append(message)
            continue

        result.append(
            {**message, "message": {**message["message"], "content": new_content}}
        )
    return result


def strip_reinjected_attachments(messages: list[Message]) -> list[Message]:
    """Strip attachment types that are re-injected post-compaction anyway.

    Currently a no-op: the skill_discovery / skill_listing attachment types are not
    produced in this build.
    """
    return messages


ERROR_MESSAGE_NOT_ENOUGH_MESSAGES = "Not enough messages to compact."
MAX_PTL_RETRIES = 3
PTL_RETRY_MARKER = "[earlier conversation truncated for compaction retry]"


def truncate_head_for_ptl_retry(
    messages: list[Message],
    ptl_response: AssistantMessage,
) -> list[Message] | None:
    """Drop the oldest API-round groups until ``tokenGap`` is covered.

    Falls back to dropping 20% of groups when the gap is unparseable. Returns
    ``None`` when nothing can be dropped without leaving an empty summarize set.
    """
    # Strip our own synthetic marker from a previous retry before grouping.
    first = messages[0] if messages else None
    if (
        first is not None
        and first.get("type") == "user"
        and first.get("isMeta")
        and first.get("message", {}).get("content") == PTL_RETRY_MARKER
    ):
        input_messages = messages[1:]
    else:
        input_messages = messages

    groups = group_messages_by_api_round(input_messages)
    if len(groups) < 2:
        return None

    token_gap = _get_prompt_too_long_token_gap(ptl_response)
    if token_gap is not None:
        acc = 0
        drop_count = 0
        for g in groups:
            acc += _rough_token_count_estimation_for_messages(g)
            drop_count += 1
            if acc >= token_gap:
                break
    else:
        import math

        drop_count = max(1, math.floor(len(groups) * 0.2))

    # Keep at least one group so there's something to summarize.
    drop_count = min(drop_count, len(groups) - 1)
    if drop_count < 1:
        return None

    sliced: list[Message] = [m for g in groups[drop_count:] for m in g]
    # group_messages_by_api_round puts the preamble in group 0 and starts every
    # subsequent group with an assistant message. Dropping group 0 leaves an
    # assistant-first sequence which the API rejects. Prepend a synthetic user
    # marker.
    if sliced and sliced[0].get("type") == "assistant":
        return [
            create_user_message(content=PTL_RETRY_MARKER, is_meta=True),
            *sliced,
        ]
    return sliced


ERROR_MESSAGE_PROMPT_TOO_LONG = (
    "Conversation too long. Press esc twice to go up a few messages and try again."
)
ERROR_MESSAGE_USER_ABORT = "API Error: Request was aborted."
ERROR_MESSAGE_INCOMPLETE_RESPONSE = (
    "Compaction interrupted · This may be due to network issues — please try again."
)


# CompactionResult is a plain dict at runtime (transcript-adjacent envelope bag).
CompactionResult = dict[str, Any]

# RecompactionInfo is a plain dict at runtime.
RecompactionInfo = dict[str, Any]


def build_post_compact_messages(result: CompactionResult) -> list[Message]:
    """Build the base post-compact messages array from a CompactionResult.

    Order: boundaryMarker, summaryMessages, messagesToKeep, attachments, hookResults.
    """
    return [
        result["boundaryMarker"],
        *result["summaryMessages"],
        *(result.get("messagesToKeep") or []),
        *result["attachments"],
        *result["hookResults"],
    ]


def annotate_boundary_with_preserved_segment(
    boundary: SystemCompactBoundaryMessage,
    anchor_uuid: str,
    messages_to_keep: list[Message] | None,
) -> SystemCompactBoundaryMessage:
    """Annotate a compact boundary with relink metadata for messagesToKeep."""
    keep = messages_to_keep or []
    if len(keep) == 0:
        return boundary
    return {
        **boundary,
        "compactMetadata": {
            **boundary["compactMetadata"],
            "preservedSegment": {
                "headUuid": keep[0]["uuid"],
                "anchorUuid": anchor_uuid,
                "tailUuid": keep[-1]["uuid"],
            },
        },
    }


def merge_hook_instructions(
    user_instructions: str | None,
    hook_instructions: str | None,
) -> str | None:
    """Merge user-supplied custom instructions with hook-provided instructions."""
    if not hook_instructions:
        return user_instructions or None
    if not user_instructions:
        return hook_instructions
    return f"{user_instructions}\n\n{hook_instructions}"


async def compact_conversation(
    messages: list[Message],
    context: ToolUseContext,
    cache_safe_params: Any,
    suppress_follow_up_questions: bool,
    custom_instructions: str | None = None,
    is_auto_compact: bool = False,
    recompaction_info: RecompactionInfo | None = None,
) -> CompactionResult:
    """Create a compact version of a conversation by summarizing older messages
    and preserving recent conversation history."""
    try:
        if len(messages) == 0:
            raise RuntimeError(ERROR_MESSAGE_NOT_ENOUGH_MESSAGES)

        pre_compact_token_count = _token_count_with_estimation(messages)

        app_state = context.get_app_state()
        _await_fire(
            log_permission_context_for_ants(
                getattr(app_state, "toolPermissionContext", None)
                if not isinstance(app_state, dict)
                else app_state.get("toolPermissionContext"),
                "summary",
            )
        )

        _on_compact_progress(context, {"type": "hooks_start", "hookType": "pre_compact"})

        # Execute PreCompact hooks.
        _call_opt(context, "set_sdk_status", "compacting")
        hook_result = await _execute_pre_compact_hooks(
            {
                "trigger": "auto" if is_auto_compact else "manual",
                "customInstructions": custom_instructions,
            },
            context.abort_controller.signal,
        )
        custom_instructions = merge_hook_instructions(
            custom_instructions,
            _hr(hook_result, "newCustomInstructions"),
        )
        user_display_message = _hr(hook_result, "userDisplayMessage")

        _call_opt(context, "set_stream_mode", "requesting")
        _call_opt(context, "set_response_length", lambda _length: 0)
        _on_compact_progress(context, {"type": "compact_start"})

        prompt_cache_sharing_enabled = _get_feature_value(
            "tengu_compact_cache_prefix", True
        )

        compact_prompt = get_compact_prompt(custom_instructions)
        summary_request = create_user_message(content=compact_prompt)

        messages_to_summarize = messages
        retry_cache_safe_params = cache_safe_params
        ptl_attempts = 0
        while True:
            summary_response = await _stream_compact_summary(
                messages=messages_to_summarize,
                summary_request=summary_request,
                app_state=app_state,
                context=context,
                pre_compact_token_count=pre_compact_token_count,
                cache_safe_params=retry_cache_safe_params,
            )
            summary = _get_assistant_message_text(summary_response)
            if not (summary and summary.startswith(PROMPT_TOO_LONG_ERROR_MESSAGE)):
                break

            # CC-1180: compact request itself hit prompt-too-long. Truncate and retry.
            ptl_attempts += 1
            truncated = (
                truncate_head_for_ptl_retry(messages_to_summarize, summary_response)
                if ptl_attempts <= MAX_PTL_RETRIES
                else None
            )
            if not truncated:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG)
            messages_to_summarize = truncated
            retry_cache_safe_params = _with_fork_context(retry_cache_safe_params, truncated)

        if not summary:
            log_for_debugging(
                f"Compact failed: no summary text in response. "
                f"Response: {json_stringify(summary_response)}",
                {"level": "error"},
            )
            raise RuntimeError(
                "Failed to generate conversation summary - response did not contain "
                "valid text content"
            )
        if starts_with_api_error_prefix(summary):
            raise RuntimeError(summary)

        # Store the current file state before clearing.
        pre_compact_read_file_state = cache_to_object(context.read_file_state)

        # Clear the cache.
        _clear(context.read_file_state)

        post_compact_file_attachments = await create_post_compact_file_attachments(
            pre_compact_read_file_state,
            context,
            POST_COMPACT_MAX_FILES_TO_RESTORE,
        )
        plan_attachment = create_plan_attachment_if_needed(context.agent_id)
        if plan_attachment:
            post_compact_file_attachments.append(plan_attachment)

        plan_mode_attachment = await create_plan_mode_attachment_if_needed(context)
        if plan_mode_attachment:
            post_compact_file_attachments.append(plan_mode_attachment)

        skill_attachment = create_skill_attachment_if_needed(context.agent_id)
        if skill_attachment:
            post_compact_file_attachments.append(skill_attachment)

        # Re-announce deltas from the current state (empty history → full set).
        for att in _get_deferred_tools_delta_attachment(
            context.options.tools,
            context.options.main_loop_model,
            [],
            call_site="compact_full",
        ):
            post_compact_file_attachments.append(_create_attachment_message(att))
        for att in _get_agent_listing_delta_attachment(context, []):
            post_compact_file_attachments.append(_create_attachment_message(att))
        for att in _get_mcp_instructions_delta_attachment(
            context.options.mcp_clients,
            context.options.tools,
            context.options.main_loop_model,
            [],
        ):
            post_compact_file_attachments.append(_create_attachment_message(att))

        _on_compact_progress(
            context, {"type": "hooks_start", "hookType": "session_start"}
        )
        # Execute SessionStart hooks after successful compaction.
        hook_messages = await _process_session_start_hooks(
            "compact", {"model": context.options.main_loop_model}
        )

        boundary_marker = _create_compact_boundary_message(
            "auto" if is_auto_compact else "manual",
            pre_compact_token_count or 0,
            messages[-1].get("uuid") if messages else None,
        )
        pre_compact_discovered = _extract_discovered_tool_names(messages)
        if len(pre_compact_discovered) > 0:
            boundary_marker["compactMetadata"]["preCompactDiscoveredTools"] = sorted(
                pre_compact_discovered
            )

        transcript_path = get_transcript_path()
        summary_messages: list[UserMessage] = [
            create_user_message(
                content=get_compact_user_summary_message(
                    summary, suppress_follow_up_questions, transcript_path
                ),
                is_compact_summary=True,
                is_visible_in_transcript_only=True,
            )
        ]

        compaction_call_total_tokens = _token_count_from_last_api_response(
            [summary_response]
        )

        true_post_compact_token_count = _rough_token_count_estimation_for_messages(
            [
                boundary_marker,
                *summary_messages,
                *post_compact_file_attachments,
                *hook_messages,
            ]
        )

        compaction_usage = _get_token_usage(summary_response)

        query_source_for_event = (
            (recompaction_info or {}).get("querySource")
            or context.options.query_source
            or "unknown"
        )

        mark_post_compaction()

        re_append_session_metadata()

        _on_compact_progress(
            context, {"type": "hooks_start", "hookType": "post_compact"}
        )
        post_compact_hook_result = await _execute_post_compact_hooks(
            {
                "trigger": "auto" if is_auto_compact else "manual",
                "compactSummary": summary,
            },
            context.abort_controller.signal,
        )

        combined_user_display_message = "\n".join(
            x
            for x in [user_display_message, _hr(post_compact_hook_result, "userDisplayMessage")]
            if x
        )

        return {
            "boundaryMarker": boundary_marker,
            "summaryMessages": summary_messages,
            "attachments": post_compact_file_attachments,
            "hookResults": hook_messages,
            "userDisplayMessage": combined_user_display_message or None,
            "preCompactTokenCount": pre_compact_token_count,
            "postCompactTokenCount": compaction_call_total_tokens,
            "truePostCompactTokenCount": true_post_compact_token_count,
            "compactionUsage": compaction_usage,
        }
    except BaseException as error:  # noqa: BLE001 — re-raised after notification
        if not is_auto_compact:
            _add_error_notification_if_needed(error, context)
        raise
    finally:
        _call_opt(context, "set_stream_mode", "requesting")
        _call_opt(context, "set_response_length", lambda _length: 0)
        _on_compact_progress(context, {"type": "compact_end"})
        _call_opt(context, "set_sdk_status", None)


async def partial_compact_conversation(
    all_messages: list[Message],
    pivot_index: int,
    context: ToolUseContext,
    cache_safe_params: Any,
    user_feedback: str | None = None,
    direction: PartialCompactDirection = "from",
) -> CompactionResult:
    """Perform a partial compaction around the selected message index."""
    try:
        messages_to_summarize = (
            all_messages[:pivot_index]
            if direction == "up_to"
            else all_messages[pivot_index:]
        )
        if direction == "up_to":
            messages_to_keep = [
                m
                for m in all_messages[pivot_index:]
                if m.get("type") != "progress"
                and not _is_compact_boundary_message(m)
                and not (m.get("type") == "user" and m.get("isCompactSummary"))
            ]
        else:
            messages_to_keep = [
                m for m in all_messages[:pivot_index] if m.get("type") != "progress"
            ]

        if len(messages_to_summarize) == 0:
            raise RuntimeError(
                "Nothing to summarize before the selected message."
                if direction == "up_to"
                else "Nothing to summarize after the selected message."
            )

        pre_compact_token_count = _token_count_with_estimation(all_messages)

        _on_compact_progress(context, {"type": "hooks_start", "hookType": "pre_compact"})

        _call_opt(context, "set_sdk_status", "compacting")
        hook_result = await _execute_pre_compact_hooks(
            {"trigger": "manual", "customInstructions": None},
            context.abort_controller.signal,
        )

        new_custom = _hr(hook_result, "newCustomInstructions")
        custom_instructions: str | None
        if new_custom and user_feedback:
            custom_instructions = f"{new_custom}\n\nUser context: {user_feedback}"
        elif new_custom:
            custom_instructions = new_custom
        elif user_feedback:
            custom_instructions = f"User context: {user_feedback}"
        else:
            custom_instructions = None

        _call_opt(context, "set_stream_mode", "requesting")
        _call_opt(context, "set_response_length", lambda _length: 0)
        _on_compact_progress(context, {"type": "compact_start"})

        compact_prompt = get_partial_compact_prompt(custom_instructions, direction)
        summary_request = create_user_message(content=compact_prompt)

        failure_metadata = {
            "preCompactTokenCount": pre_compact_token_count,
            "direction": direction,
            "messagesSummarized": len(messages_to_summarize),
        }

        api_messages = (
            messages_to_summarize if direction == "up_to" else all_messages
        )
        retry_cache_safe_params = (
            _with_fork_context(cache_safe_params, messages_to_summarize)
            if direction == "up_to"
            else cache_safe_params
        )
        ptl_attempts = 0
        while True:
            summary_response = await _stream_compact_summary(
                messages=api_messages,
                summary_request=summary_request,
                app_state=context.get_app_state(),
                context=context,
                pre_compact_token_count=pre_compact_token_count,
                cache_safe_params=retry_cache_safe_params,
            )
            summary = _get_assistant_message_text(summary_response)
            if not (summary and summary.startswith(PROMPT_TOO_LONG_ERROR_MESSAGE)):
                break

            ptl_attempts += 1
            truncated = (
                truncate_head_for_ptl_retry(api_messages, summary_response)
                if ptl_attempts <= MAX_PTL_RETRIES
                else None
            )
            if not truncated:
                raise RuntimeError(ERROR_MESSAGE_PROMPT_TOO_LONG)
            api_messages = truncated
            retry_cache_safe_params = _with_fork_context(retry_cache_safe_params, truncated)

        if not summary:
            raise RuntimeError(
                "Failed to generate conversation summary - response did not contain "
                "valid text content"
            )
        if starts_with_api_error_prefix(summary):
            raise RuntimeError(summary)

        pre_compact_read_file_state = cache_to_object(context.read_file_state)
        _clear(context.read_file_state)

        post_compact_file_attachments = await create_post_compact_file_attachments(
            pre_compact_read_file_state,
            context,
            POST_COMPACT_MAX_FILES_TO_RESTORE,
            messages_to_keep,
        )
        plan_attachment = create_plan_attachment_if_needed(context.agent_id)
        if plan_attachment:
            post_compact_file_attachments.append(plan_attachment)

        plan_mode_attachment = await create_plan_mode_attachment_if_needed(context)
        if plan_mode_attachment:
            post_compact_file_attachments.append(plan_mode_attachment)

        skill_attachment = create_skill_attachment_if_needed(context.agent_id)
        if skill_attachment:
            post_compact_file_attachments.append(skill_attachment)

        for att in _get_deferred_tools_delta_attachment(
            context.options.tools,
            context.options.main_loop_model,
            messages_to_keep,
            call_site="compact_partial",
        ):
            post_compact_file_attachments.append(_create_attachment_message(att))
        for att in _get_agent_listing_delta_attachment(context, messages_to_keep):
            post_compact_file_attachments.append(_create_attachment_message(att))
        for att in _get_mcp_instructions_delta_attachment(
            context.options.mcp_clients,
            context.options.tools,
            context.options.main_loop_model,
            messages_to_keep,
        ):
            post_compact_file_attachments.append(_create_attachment_message(att))

        _on_compact_progress(
            context, {"type": "hooks_start", "hookType": "session_start"}
        )
        hook_messages = await _process_session_start_hooks(
            "compact", {"model": context.options.main_loop_model}
        )

        post_compact_token_count = _token_count_from_last_api_response([summary_response])
        compaction_usage = _get_token_usage(summary_response)

        if direction == "up_to":
            last_pre = next(
                (
                    m
                    for m in reversed(all_messages[:pivot_index])
                    if m.get("type") != "progress"
                ),
                None,
            )
            last_pre_compact_uuid = last_pre.get("uuid") if last_pre else None
        else:
            last_pre_compact_uuid = (
                messages_to_keep[-1].get("uuid") if messages_to_keep else None
            )
        boundary_marker = _create_compact_boundary_message(
            "manual",
            pre_compact_token_count or 0,
            last_pre_compact_uuid,
            user_feedback,
            len(messages_to_summarize),
        )
        pre_compact_discovered = _extract_discovered_tool_names(all_messages)
        if len(pre_compact_discovered) > 0:
            boundary_marker["compactMetadata"]["preCompactDiscoveredTools"] = sorted(
                pre_compact_discovered
            )

        transcript_path = get_transcript_path()
        extra_kwargs: dict[str, Any] = (
            {
                "summarize_metadata": {
                    "messagesSummarized": len(messages_to_summarize),
                    "userContext": user_feedback,
                    "direction": direction,
                }
            }
            if len(messages_to_keep) > 0
            else {"is_visible_in_transcript_only": True}
        )
        summary_messages = [
            create_user_message(
                content=get_compact_user_summary_message(summary, False, transcript_path),
                is_compact_summary=True,
                **extra_kwargs,
            )
        ]

        mark_post_compaction()

        re_append_session_metadata()

        _on_compact_progress(
            context, {"type": "hooks_start", "hookType": "post_compact"}
        )
        post_compact_hook_result = await _execute_post_compact_hooks(
            {"trigger": "manual", "compactSummary": summary},
            context.abort_controller.signal,
        )

        # 'from': prefix-preserving → boundary; 'up_to': suffix → last summary.
        if direction == "up_to":
            anchor_uuid = (
                summary_messages[-1].get("uuid") if summary_messages else None
            ) or boundary_marker["uuid"]
        else:
            anchor_uuid = boundary_marker["uuid"]

        return {
            "boundaryMarker": annotate_boundary_with_preserved_segment(
                boundary_marker, anchor_uuid, messages_to_keep
            ),
            "summaryMessages": summary_messages,
            "messagesToKeep": messages_to_keep,
            "attachments": post_compact_file_attachments,
            "hookResults": hook_messages,
            "userDisplayMessage": _hr(post_compact_hook_result, "userDisplayMessage"),
            "preCompactTokenCount": pre_compact_token_count,
            "postCompactTokenCount": post_compact_token_count,
            "compactionUsage": compaction_usage,
        }
    except BaseException as error:  # noqa: BLE001 — re-raised after notification
        _add_error_notification_if_needed(error, context)
        raise
    finally:
        _call_opt(context, "set_stream_mode", "requesting")
        _call_opt(context, "set_response_length", lambda _length: 0)
        _on_compact_progress(context, {"type": "compact_end"})
        _call_opt(context, "set_sdk_status", None)


def _add_error_notification_if_needed(error: Any, context: ToolUseContext) -> None:
    if not _exact(error, ERROR_MESSAGE_USER_ABORT) and not _exact(
        error, ERROR_MESSAGE_NOT_ENOUGH_MESSAGES
    ):
        add = getattr(context, "add_notification", None)
        if add is not None:
            add(
                {
                    "key": "error-compacting-conversation",
                    "text": "Error compacting conversation",
                    "priority": "immediate",
                    "color": "error",
                }
            )


def create_compact_can_use_tool() -> CanUseToolFn:
    async def _can_use_tool(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "behavior": "deny",
            "message": "Tool use is not allowed during compaction",
            "decisionReason": {
                "type": "other",
                "reason": "compaction agent should only produce text summary",
            },
        }

    return _can_use_tool


async def _stream_compact_summary(
    *,
    messages: list[Message],
    summary_request: UserMessage,
    app_state: Any,
    context: ToolUseContext,
    pre_compact_token_count: int,
    cache_safe_params: Any,
) -> AssistantMessage:
    prompt_cache_sharing_enabled = _get_feature_value("tengu_compact_cache_prefix", True)

    # Keep-alive signals during compaction to prevent remote session WebSocket
    # idle timeouts. Two signals: heartbeat + re-emit 'compacting' status.
    import asyncio

    activity_task: asyncio.Task[Any] | None = None
    if is_session_activity_tracking_active():
        status_setter = getattr(context, "set_sdk_status", None)

        async def _keepalive() -> None:
            try:
                while True:
                    await asyncio.sleep(30)
                    send_session_activity_signal()
                    if status_setter is not None:
                        status_setter("compacting")
            except asyncio.CancelledError:
                return

        activity_task = asyncio.ensure_future(_keepalive())

    try:
        if prompt_cache_sharing_enabled:
            try:
                from tabvis.utils.forked_agent import run_forked_agent

                result = await run_forked_agent(
                    {
                        "promptMessages": [summary_request],
                        "cacheSafeParams": cache_safe_params,
                        "canUseTool": create_compact_can_use_tool(),
                        "querySource": "compact",
                        "forkLabel": "compact",
                        "maxTurns": 1,
                        "skipCacheWrite": True,
                        "overrides": {"abortController": context.abort_controller},
                    }
                )
                assistant_msg = _get_last_assistant_message(result["messages"])
                assistant_text = (
                    _get_assistant_message_text(assistant_msg) if assistant_msg else None
                )
                if (
                    assistant_msg
                    and assistant_text
                    and not assistant_msg.get("isApiErrorMessage")
                ):
                    return assistant_msg
                log_for_debugging(
                    f"Compact cache sharing: no text in response, falling back. "
                    f"Response: {json_stringify(assistant_msg)}",
                    {"level": "warn"},
                )
            except Exception as error:  # noqa: BLE001
                log_error(error)

        # Regular streaming path (fallback when cache sharing fails or is disabled).
        retry_enabled = _get_feature_value("tengu_compact_streaming_retry", False)
        max_attempts = MAX_COMPACT_STREAMING_RETRIES if retry_enabled else 1

        for attempt in range(1, max_attempts + 1):
            has_started_streaming = False
            response: AssistantMessage | None = None
            _call_opt(context, "set_response_length", lambda _length: 0)

            use_tool_search = await _is_tool_search_enabled(
                context.options.main_loop_model,
                context.options.tools,
                _tool_permission_context_getter(context),
                _active_agents(context),
                "compact",
            )

            # Lazy: ToolSearchTool is a cycle sibling; FileReadTool is imported
            # here to keep the module import standalone.
            from tabvis.agent.tools.file_read_tool import FileReadTool

            if use_tool_search:
                from tabvis.agent.tools.tool_search_tool import ToolSearchTool

                tools = _uniq_by_name(
                    [
                        FileReadTool,
                        ToolSearchTool,
                        *[t for t in context.options.tools if getattr(t, "is_mcp", False)],
                    ]
                )
            else:
                tools = [FileReadTool]

            from tabvis.agent.api.model_client import Options

            streaming_gen = _query_model_with_streaming(
                messages=normalize_messages_for_api(
                    strip_images_from_messages(
                        strip_reinjected_attachments(
                            [
                                *_get_messages_after_compact_boundary(messages),
                                summary_request,
                            ]
                        )
                    ),
                    context.options.tools,
                ),
                system_prompt=as_system_prompt(
                    ["You are a helpful AI assistant tasked with summarizing conversations."]
                ),
                thinking_config={"type": "disabled"},
                tools=tools,
                signal=context.abort_controller.signal,
                # query_model_with_streaming requires an Options DATACLASS (it does options.model,
                # options.query_source, options.enable_prompt_caching, … attribute access); passing a
                # camelCase dict here used to AttributeError on the first access, silently disabling
                # every compaction (auto_compact_if_needed swallowed it and tripped its breaker). The
                # four dict keys with no Options field (toolChoice / hasAppendSystemPrompt / mcpTools /
                # effortValue) are not read by the callee and are dropped; agent_id comes off the
                # ToolUseContext, not options.
                options=Options(
                    model=context.options.main_loop_model,
                    get_tool_permission_context=_tool_permission_context_getter(context),
                    is_non_interactive_session=context.options.is_non_interactive_session,
                    query_source="compact",
                    agents=_active_agents(context),
                    max_output_tokens_override=min(
                        COMPACT_MAX_OUTPUT_TOKENS,
                        _get_max_output_tokens_for_model(context.options.main_loop_model),
                    ),
                    agent_id=context.agent_id,
                ),
            )

            async for event in streaming_gen:
                etype = event.get("type")
                if (
                    not has_started_streaming
                    and etype == "stream_event"
                    and _ev(event, "type") == "content_block_start"
                    and _ev(event, "content_block", "type") == "text"
                ):
                    has_started_streaming = True
                    _call_opt(context, "set_stream_mode", "responding")

                if (
                    etype == "stream_event"
                    and _ev(event, "type") == "content_block_delta"
                    and _ev(event, "delta", "type") == "text_delta"
                ):
                    characters_streamed = len(_ev(event, "delta", "text") or "")
                    _call_opt(
                        context,
                        "set_response_length",
                        lambda length, c=characters_streamed: length + c,
                    )

                if etype == "assistant":
                    response = event

            if response is not None:
                return response

            if attempt < max_attempts:
                from tabvis.utils.abort import AbortError

                await sleep(
                    get_retry_delay(attempt),
                    context.abort_controller.signal,
                    abort_error=lambda: AbortError(),
                )
                continue

            log_for_debugging(
                f"Compact streaming failed after {attempt} attempts. "
                f"hasStartedStreaming={has_started_streaming}",
                {"level": "error"},
            )
            raise RuntimeError(ERROR_MESSAGE_INCOMPLETE_RESPONSE)

        raise RuntimeError(ERROR_MESSAGE_INCOMPLETE_RESPONSE)
    finally:
        if activity_task is not None:
            activity_task.cancel()


async def create_post_compact_file_attachments(
    read_file_state: dict[str, dict[str, Any]],
    tool_use_context: ToolUseContext,
    max_files: int,
    preserved_messages: list[Message] | None = None,
) -> list[AttachmentMessage]:
    """Create attachment messages for recently accessed files to restore them
    after compaction. Files already present as Read tool results in
    ``preserved_messages`` are skipped."""
    preserved_messages = preserved_messages or []
    preserved_read_paths = _collect_read_tool_file_paths(preserved_messages)
    recent_files = [
        {"filename": filename, **state}
        for filename, state in read_file_state.items()
    ]
    recent_files = [
        f
        for f in recent_files
        if not _should_exclude_from_post_compact_restore(
            f["filename"], tool_use_context.agent_id
        )
        and expand_path(f["filename"]) not in preserved_read_paths
    ]
    recent_files.sort(key=lambda f: f["timestamp"], reverse=True)
    recent_files = recent_files[:max_files]

    import asyncio

    async def _gen(file: dict[str, Any]) -> AttachmentMessage | None:
        attachment = await _generate_file_attachment(
            file["filename"],
            {
                **_context_as_dict(tool_use_context),
                "fileReadingLimits": {"maxTokens": POST_COMPACT_MAX_TOKENS_PER_FILE},
            },
            "tengu_post_compact_file_restore_success",
            "tengu_post_compact_file_restore_error",
            "compact",
        )
        return _create_attachment_message(attachment) if attachment else None

    results = await asyncio.gather(*[_gen(f) for f in recent_files])

    used_tokens = 0
    kept: list[AttachmentMessage] = []
    for result in results:
        if result is None:
            continue
        attachment_tokens = _rough_token_count_estimation(json_stringify(result))
        if used_tokens + attachment_tokens <= POST_COMPACT_TOKEN_BUDGET:
            used_tokens += attachment_tokens
            kept.append(result)
    return kept


def create_plan_attachment_if_needed(
    agent_id: AgentId | None = None,
) -> AttachmentMessage | None:
    """Create a plan file attachment if a plan file exists for the current session."""
    plan_content = get_plan(agent_id)
    if not plan_content:
        return None

    plan_file_path = get_plan_file_path(agent_id)
    return _create_attachment_message(
        {
            "type": "plan_file_reference",
            "planFilePath": plan_file_path,
            "planContent": plan_content,
        }
    )


def create_skill_attachment_if_needed(
    agent_id: str | None = None,
) -> AttachmentMessage | None:
    """Create an attachment for invoked skills to preserve content across compaction."""
    invoked_skills = get_invoked_skills_for_agent(agent_id)
    if len(invoked_skills) == 0:
        return None

    used_tokens = 0
    skills_sorted = sorted(
        invoked_skills.values(), key=lambda s: s["invokedAt"], reverse=True
    )
    skills: list[dict[str, Any]] = []
    for skill in skills_sorted:
        entry = {
            "name": skill["skillName"],
            "path": skill["skillPath"],
            "content": _truncate_to_tokens(
                skill["content"], POST_COMPACT_MAX_TOKENS_PER_SKILL
            ),
        }
        tokens = _rough_token_count_estimation(entry["content"])
        if used_tokens + tokens > POST_COMPACT_SKILLS_TOKEN_BUDGET:
            continue
        used_tokens += tokens
        skills.append(entry)

    if len(skills) == 0:
        return None

    return _create_attachment_message({"type": "invoked_skills", "skills": skills})


async def create_plan_mode_attachment_if_needed(
    context: ToolUseContext,
) -> AttachmentMessage | None:
    """Create a plan_mode attachment if the user is currently in plan mode."""
    app_state = context.get_app_state()
    permission_context = _app_state_get(app_state, "toolPermissionContext") or {}
    mode = (
        permission_context.get("mode")
        if isinstance(permission_context, dict)
        else getattr(permission_context, "mode", None)
    )
    if mode != "plan":
        return None

    plan_file_path = get_plan_file_path(context.agent_id)
    plan_exists = get_plan(context.agent_id) is not None

    return _create_attachment_message(
        {
            "type": "plan_mode",
            "reminderType": "full",
            "isSubAgent": bool(context.agent_id),
            "planFilePath": plan_file_path,
            "planExists": plan_exists,
        }
    )


def _collect_read_tool_file_paths(messages: list[Message]) -> set[str]:
    """Scan messages for Read tool_use blocks and collect their file_path inputs."""
    from tabvis.agent.tools.file_read_tool import FILE_READ_TOOL_NAME, FILE_UNCHANGED_STUB

    stub_ids: set[str] = set()
    for message in messages:
        content = message.get("message", {}).get("content")
        if message.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if (
                block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
                and block["content"].startswith(FILE_UNCHANGED_STUB)
            ):
                stub_ids.add(block["tool_use_id"])

    paths: set[str] = set()
    for message in messages:
        content = message.get("message", {}).get("content")
        if message.get("type") != "assistant" or not isinstance(content, list):
            continue
        for block in content:
            if (
                block.get("type") != "tool_use"
                or block.get("name") != FILE_READ_TOOL_NAME
                or block.get("id") in stub_ids
            ):
                continue
            input_ = block.get("input")
            if (
                input_
                and isinstance(input_, dict)
                and isinstance(input_.get("file_path"), str)
            ):
                paths.add(expand_path(input_["file_path"]))
    return paths


SKILL_TRUNCATION_MARKER = (
    "\n\n[... skill content truncated for compaction; use Read on the skill path "
    "if you need the full text]"
)


def _truncate_to_tokens(content: str, max_tokens: int) -> str:
    """Truncate content to roughly ``max_tokens``, keeping the head."""
    if _rough_token_count_estimation(content) <= max_tokens:
        return content
    char_budget = max_tokens * 4 - len(SKILL_TRUNCATION_MARKER)
    return content[:char_budget] + SKILL_TRUNCATION_MARKER


def _should_exclude_from_post_compact_restore(
    filename: str,
    agent_id: AgentId | None = None,
) -> bool:
    normalized_filename = expand_path(filename)
    # Exclude plan files.
    try:
        plan_file_path = expand_path(get_plan_file_path(agent_id))
        if normalized_filename == plan_file_path:
            return True
    except Exception:  # noqa: BLE001
        pass

    # Exclude all types of tabvis.md files.
    try:
        from tabvis.utils.config import get_memory_path  # type: ignore[attr-defined]

        normalized_memory_paths = {
            expand_path(get_memory_path(t)) for t in MEMORY_TYPE_VALUES
        }
        if normalized_filename in normalized_memory_paths:
            return True
    except Exception:
        pass

    return False


# --- tiny internal helpers -----------------------------------------------------------------------


def _await_fire(coro: Any) -> None:
    """Fire-and-forget an awaitable."""
    import asyncio

    if not hasattr(coro, "__await__"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(coro)
        except Exception:  # noqa: BLE001
            pass
        return
    loop.create_task(coro)


def _hr(hook_result: Any, key: str) -> Any:
    if hook_result is None:
        return None
    if isinstance(hook_result, dict):
        return hook_result.get(key)
    return getattr(hook_result, key, None)


def _exact(error: Any, message: str) -> bool:
    return isinstance(error, BaseException) and str(error) == message


def _clear(state: Any) -> None:
    if hasattr(state, "clear"):
        state.clear()


def _with_fork_context(params: Any, messages: list[Message]) -> Any:
    """Return ``params`` with ``forkContextMessages`` set to ``messages``.

    Supports both dataclass-style ``CacheSafeParams`` (``.fork_context_messages``)
    and plain dicts.
    """
    if params is None:
        return params
    if isinstance(params, dict):
        return {**params, "forkContextMessages": messages}
    try:
        import dataclasses

        if dataclasses.is_dataclass(params):
            return dataclasses.replace(params, fork_context_messages=messages)
    except (TypeError, ValueError):
        pass
    # Last resort: mutate a copy.
    import copy

    new = copy.copy(params)
    if hasattr(new, "fork_context_messages"):
        new.fork_context_messages = messages
    return new


def _qt(context: ToolUseContext, attr: str, default: Any = None) -> Any:
    qt = getattr(context, "query_tracking", None)
    if qt is None:
        return default
    if isinstance(qt, dict):
        return qt.get(attr, default)
    return getattr(qt, attr, default)


def _usage(usage: Any, key: str) -> Any:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _safe_token_stats(messages: list[Message]) -> dict[str, Any]:
    try:
        return _token_stats_to_statsig_metrics(_analyze_context(messages))
    except Exception as error:  # noqa: BLE001
        log_error(error)
        return {}


def _ev(event: dict[str, Any], *path: str) -> Any:
    cur: Any = event.get("event")
    for key in path:
        if cur is None:
            return None
        cur = cur.get(key) if isinstance(cur, dict) else getattr(cur, key, None)
    return cur


def _active_agents(context: ToolUseContext) -> Any:
    agent_definitions = context.options.agent_definitions
    if agent_definitions is None:
        return []
    if isinstance(agent_definitions, dict):
        return agent_definitions.get("activeAgents", [])
    return getattr(agent_definitions, "active_agents", getattr(agent_definitions, "activeAgents", []))


def _tool_permission_context_getter(context: ToolUseContext) -> Callable[[], Any]:
    async def _getter() -> Any:
        app_state = context.get_app_state()
        return _app_state_get(app_state, "toolPermissionContext")

    return _getter


def _app_state_get(app_state: Any, key: str) -> Any:
    if app_state is None:
        return None
    if isinstance(app_state, dict):
        return app_state.get(key)
    return getattr(app_state, key, None)


def _context_as_dict(context: ToolUseContext) -> dict[str, Any]:
    """Spread the context into a dict for ``generate_file_attachment``'s options bag."""
    return {
        "options": context.options,
        "abortController": context.abort_controller,
        "readFileState": context.read_file_state,
        "getAppState": context.get_app_state,
        "agentId": context.agent_id,
    }


def _uniq_by_name(items: list[Any]) -> list[Any]:
    """Deduplicate by ``.name``, keeping the first occurrence of each."""
    seen: set[str] = set()
    result: list[Any] = []
    for item in items:
        name = getattr(item, "name", None)
        if name in seen:
            continue
        seen.add(name)
        result.append(item)
    return result


def _g(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
