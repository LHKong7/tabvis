"""Large tool-result spillover store

Persists oversized tool results to disk (under ``<projectDir>/<sessionId>/tool-results/``)
instead of truncating them, replacing the in-prompt content with a short reference + preview so
the model can ``Read`` the full output back. Also implements the per-message aggregate
tool-result budget (``ContentReplacementState`` / ``enforceToolResultBudget``) that freezes
replacement decisions across turns for prompt-cache stability.

Casing rule (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; dict-shaped
data that round-trips to the transcript / Anthropic wire keeps its wire keys. The
``ContentReplacementRecord`` envelope therefore keeps its keys verbatim
(``kind`` / ``toolUseId`` / ``replacement``) — reused from :mod:`tabvis.types.logs`. Anthropic
tool_result blocks keep their snake_case wire keys (``tool_use_id`` / ``is_error`` / ``type`` /
``text`` / ``content``). The in-process ``ContentReplacementState`` is a runtime object (a set +
a dict), never serialized, so its attributes are snake_case identifiers.

Cyclic-group note (this module participates in the
``session_storage <-> file_history <-> tool_result_storage <-> graceful_shutdown`` cycle): the
TS imports ``getProjectDir`` from ``./sessionStorage.js`` (a cyclic sibling). The identical
``get_project_dir`` is also available standalone on :mod:`tabvis.utils.session_storage_portable`
(existing, no cycle), so it is imported from there — keeping this module
import-standalone. No top-level import of a cyclic sibling exists.

fs/promises is replaced by :func:`asyncio.to_thread` over stdlib ``os`` (the
``tabvis.utils.fs_operations`` house style). ``slowOperations.jsonStringify`` →
``tabvis.utils.slow_operations.json_stringify``. The analytics metadata subsystem is not
implemented in this build; ``sanitize_tool_name_for_analytics`` is a local no-op identity.
"""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any, TypedDict

from tabvis.bootstrap.state import get_original_cwd, get_session_id
from tabvis.constants.tool_limits import (
    DEFAULT_MAX_RESULT_SIZE_CHARS,
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULTS_PER_MESSAGE_CHARS)
from tabvis.types.logs import ContentReplacementRecord
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import get_errno_code
from tabvis.utils.format import format_file_size
from tabvis.utils.log import log_error
from tabvis.utils.session_storage_portable import get_project_dir
from tabvis.utils.slow_operations import json_stringify

# Subdirectory name for tool results within a session.
TOOL_RESULTS_SUBDIR = "tool-results"

# XML tag used to wrap persisted output messages.
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

# Message used when tool result content was cleared without persisting to file.
TOOL_RESULT_CLEARED_MESSAGE = "[Old tool result content cleared]"

# Preview size in bytes for the reference message.
PREVIEW_SIZE_BYTES = 2000

# GrowthBook override map: tool name -> persistence threshold (chars).
_PERSIST_THRESHOLD_OVERRIDE_FLAG = "tengu_satin_quoll"


def _is_finite(value: float) -> bool:
    """``Number.isFinite`` — True for a real finite number (rejects inf/nan)."""
    return isinstance(value, (int, float)) and math.isfinite(value)


def sanitize_tool_name_for_analytics(tool_name: str) -> str:
    """No-op identity for tool-name analytics sanitization.

    The analytics metadata subsystem is not implemented in this build. Returns the
    name unchanged.
    """
    return tool_name


def get_persistence_threshold(
    tool_name: str, declared_max_result_size_chars: float
) -> float:
    """Resolve the effective persistence threshold for a tool (GrowthBook override wins)."""
    # Infinity = hard opt-out (e.g. Read self-bounds via maxTokens). Checked before the GB
    # override so the flag can't force it back on.
    if not _is_finite(declared_max_result_size_chars):
        return declared_max_result_size_chars
    overrides = {}
    override = overrides.get(tool_name) if isinstance(overrides, Mapping) else None
    if isinstance(override, (int, float)) and _is_finite(override) and override > 0:
        return override
    return min(declared_max_result_size_chars, DEFAULT_MAX_RESULT_SIZE_CHARS)


class PersistedToolResult(TypedDict):
    """Result of persisting a tool result to disk."""

    filepath: str
    originalSize: int
    isJson: bool
    preview: str
    hasMore: bool


class PersistToolResultError(TypedDict):
    """Error result when persistence fails."""

    error: str


def _get_session_dir() -> str:
    """The session directory (``projectDir/sessionId``)."""
    return os.path.join(get_project_dir(get_original_cwd()), get_session_id())


def get_tool_results_dir() -> str:
    """The tool-results directory for this session (``projectDir/sessionId/tool-results``)."""
    return os.path.join(_get_session_dir(), TOOL_RESULTS_SUBDIR)


def get_tool_result_path(result_id: str, is_json: bool) -> str:
    """The filepath where a tool result would be persisted."""
    ext = "json" if is_json else "txt"
    return os.path.join(get_tool_results_dir(), f"{result_id}.{ext}")


async def ensure_tool_results_dir() -> None:
    """Ensure the session-specific tool results directory exists."""
    try:
        await asyncio.to_thread(
            os.makedirs, get_tool_results_dir(), exist_ok=True
        )
    except Exception:  # noqa: BLE001
        pass  # directory may already exist


async def persist_tool_result(
    content: str | list[dict[str, Any]],
    tool_use_id: str,
) -> PersistedToolResult | PersistToolResultError:
    """Persist a tool result to disk; return info about the persisted file or an error."""
    is_json = isinstance(content, list)

    # We can only persist text blocks.
    if is_json:
        has_non_text_content = any(
            block.get("type") != "text" for block in content
        )
        if has_non_text_content:
            return {
                "error": "Cannot persist tool results containing non-text content"
            }

    await ensure_tool_results_dir()
    filepath = get_tool_result_path(tool_use_id, is_json)
    content_str = json_stringify(content, None, 2) if is_json else content

    # tool_use_id is unique per invocation and content is deterministic, so skip if the file
    # already exists. 'wx' = exclusive create (fails with EEXIST instead of overwriting).
    try:
        await asyncio.to_thread(_write_exclusive, filepath, content_str)
        log_for_debugging(
            f"Persisted tool result to {filepath} "
            f"({format_file_size(len(content_str))})"
        )
    except FileExistsError:
        pass  # EEXIST: already persisted on a prior turn, fall through to preview
    except OSError as error:
        if get_errno_code(error) != "EEXIST":
            log_error(error)
            return {"error": _get_file_system_error_message(error)}

    preview, has_more = generate_preview(content_str, PREVIEW_SIZE_BYTES)

    return {
        "filepath": filepath,
        "originalSize": len(content_str),
        "isJson": is_json,
        "preview": preview,
        "hasMore": has_more,
    }


def _write_exclusive(filepath: str, content_str: str) -> None:
    """Write with O_EXCL semantics ('wx' flag) — raise ``FileExistsError`` if it already exists."""
    fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content_str)
    except BaseException:
        # fdopen took ownership of fd; if it failed before that we still close.
        raise


def build_large_tool_result_message(result: PersistedToolResult) -> str:
    """Build a message for large tool results with preview."""
    message = f"{PERSISTED_OUTPUT_TAG}\n"
    message += (
        f"Output too large ({format_file_size(result['originalSize'])}). "
        f"Full output saved to: {result['filepath']}\n\n"
    )
    message += f"Preview (first {format_file_size(PREVIEW_SIZE_BYTES)}):\n"
    message += result["preview"]
    message += "\n...\n" if result["hasMore"] else "\n"
    message += PERSISTED_OUTPUT_CLOSING_TAG
    return message


async def process_tool_result_block(
    tool: Any,
    tool_use_result: Any,
    tool_use_id: str,
) -> dict[str, Any]:
    """Map a tool result to the API format and persist large results to disk.

    ``tool`` exposes ``name``, ``max_result_size_chars`` and
    ``map_tool_result_to_tool_result_block_param(result, tool_use_id)``.
    """
    tool_result_block = tool.map_tool_result_to_tool_result_block_param(
        tool_use_result, tool_use_id
    )
    return await _maybe_persist_large_tool_result(
        tool_result_block,
        tool.name,
        get_persistence_threshold(tool.name, tool.max_result_size_chars),
    )


async def process_pre_mapped_tool_result_block(
    tool_result_block: dict[str, Any],
    tool_name: str,
    max_result_size_chars: float,
) -> dict[str, Any]:
    """Apply persistence to a pre-mapped tool result block (no re-mapping)."""
    return await _maybe_persist_large_tool_result(
        tool_result_block,
        tool_name,
        get_persistence_threshold(tool_name, max_result_size_chars),
    )


def is_tool_result_content_empty(content: Any) -> bool:
    """True when a tool_result's content is empty or effectively empty.

    Covers ``None``/``''``, whitespace-only strings, empty arrays, and arrays whose only blocks
    are text blocks with empty/whitespace text. Non-text blocks (images, references) are
    non-empty.
    """
    if not content:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    if not isinstance(content, list):
        return False
    if len(content) == 0:
        return True
    return all(
        isinstance(block, dict)
        and block.get("type") == "text"
        and (
            not isinstance(block.get("text"), str)
            or block.get("text", "").strip() == ""
        )
        for block in content
    )


async def _maybe_persist_large_tool_result(
    tool_result_block: dict[str, Any],
    tool_name: str,
    persistence_threshold: float | None = None,
) -> dict[str, Any]:
    """Persist large tool results to disk instead of truncating."""
    content = tool_result_block.get("content")

    # Empty tool_result content at the prompt tail can make some models end their turn with zero
    # output — inject a short marker so the model always has something to react to.
    if is_tool_result_content_empty(content):
        return {
            **tool_result_block,
            "content": f"({tool_name} completed with no output)",
        }
    if not content:
        return tool_result_block

    # Skip persistence for image content blocks.
    if _has_image_block(content):
        return tool_result_block

    size = _content_size(content)
    threshold = persistence_threshold if persistence_threshold is not None else (
        MAX_TOOL_RESULT_BYTES
    )
    if size <= threshold:
        return tool_result_block

    result = await persist_tool_result(content, tool_result_block["tool_use_id"])
    if is_persist_error(result):
        return tool_result_block

    message = build_large_tool_result_message(result)

    return {**tool_result_block, "content": message}


def generate_preview(content: str, max_bytes: int) -> tuple[str, bool]:
    """Generate a preview of content, truncating at a newline boundary when possible.

    Returns ``(preview, has_more)``.
    """
    if len(content) <= max_bytes:
        return content, False

    truncated = content[:max_bytes]
    last_newline = truncated.rfind("\n")
    cut_point = last_newline if last_newline > max_bytes * 0.5 else max_bytes
    return content[:cut_point], True


def is_persist_error(
    result: PersistedToolResult | PersistToolResultError,
) -> bool:
    """Type guard: is the persist result an error?"""
    return "error" in result


# --- Message-level aggregate tool result budget ---


@dataclass
class ContentReplacementState:
    """Per-conversation-thread state for the aggregate tool-result budget.

    - ``seen_ids``: results that have passed through the budget check (replaced or not). Once
      seen, a result's fate is frozen for the conversation.
    - ``replacements``: subset of ``seen_ids`` persisted to disk + replaced with previews, mapped
      to the exact preview string shown to the model. Re-application is a dict lookup.
    """

    seen_ids: set[str] = field(default_factory=set)
    replacements: dict[str, str] = field(default_factory=dict)


def create_content_replacement_state() -> ContentReplacementState:
    return ContentReplacementState(seen_ids=set(), replacements={})


def clone_content_replacement_state(
    source: ContentReplacementState,
) -> ContentReplacementState:
    """Clone replacement state for a cache-sharing fork (mutating the clone won't affect source)."""
    return ContentReplacementState(
        seen_ids=set(source.seen_ids),
        replacements=dict(source.replacements),
    )


def get_per_message_budget_limit() -> int:
    """Resolve the per-message aggregate budget limit (GrowthBook override wins)."""
    override = None
    if isinstance(override, (int, float)) and _is_finite(override) and override > 0:
        return override
    return MAX_TOOL_RESULTS_PER_MESSAGE_CHARS


def provision_content_replacement_state(
    initial_messages: list[dict[str, Any]] | None = None,
    initial_content_replacements: list[ContentReplacementRecord] | None = None,
) -> ContentReplacementState | None:
    """Provision replacement state for a new conversation thread (feature-flag gated)."""
    enabled = False
    if not enabled:
        return None
    if initial_messages is not None:
        return reconstruct_content_replacement_state(
            initial_messages, initial_content_replacements or []
        )
    return create_content_replacement_state()


# ToolResultReplacementRecord is the tool-result kind of ContentReplacementRecord.
ToolResultReplacementRecord = ContentReplacementRecord


@dataclass
class _ToolResultCandidate:
    tool_use_id: str
    content: str | list[dict[str, Any]]
    size: int


@dataclass
class _CandidatePartition:
    must_reapply: list[tuple[_ToolResultCandidate, str]] = field(default_factory=list)
    frozen: list[_ToolResultCandidate] = field(default_factory=list)
    fresh: list[_ToolResultCandidate] = field(default_factory=list)


def _is_content_already_compacted(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(PERSISTED_OUTPUT_TAG)


def _has_image_block(content: Any) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "image" for b in content
    )


def _content_size(content: str | list[dict[str, Any]]) -> int:
    if isinstance(content, str):
        return len(content)
    # Sum text-block lengths directly (rough token heuristic).
    return sum(
        len(b.get("text", "")) if b.get("type") == "text" else 0 for b in content
    )


def _build_tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Walk messages and build tool_use_id -> tool_name from assistant tool_use blocks."""
    name_map: dict[str, str] = {}
    for message in messages:
        if message.get("type") != "assistant":
            continue
        content = message.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                name_map[block["id"]] = block["name"]
    return name_map


def _collect_candidates_from_message(
    message: dict[str, Any],
) -> list[_ToolResultCandidate]:
    """Extract eligible tool_result blocks from a single user message."""
    if message.get("type") != "user":
        return []
    content = message.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    candidates: list[_ToolResultCandidate] = []
    for block in content:
        if block.get("type") != "tool_result" or not block.get("content"):
            continue
        if _is_content_already_compacted(block["content"]):
            continue
        if _has_image_block(block["content"]):
            continue
        candidates.append(
            _ToolResultCandidate(
                tool_use_id=block["tool_use_id"],
                content=block["content"],
                size=_content_size(block["content"]),
            )
        )
    return candidates


def _collect_candidates_by_message(
    messages: list[dict[str, Any]],
) -> list[list[_ToolResultCandidate]]:
    """Extract candidate tool_result blocks grouped by API-level user message.

    A "group" is a maximal run of user messages NOT separated by a distinct-id assistant
    message (mirroring how ``normalizeMessagesForAPI`` merges consecutive user messages). Only
    groups with at least one eligible candidate are returned.
    """
    groups: list[list[_ToolResultCandidate]] = []
    current: list[_ToolResultCandidate] = []

    def flush() -> None:
        nonlocal current
        if len(current) > 0:
            groups.append(current)
        current = []

    seen_asst_ids: set[str] = set()
    for message in messages:
        msg_type = message.get("type")
        if msg_type == "user":
            current.extend(_collect_candidates_from_message(message))
        elif msg_type == "assistant":
            asst_id = message.get("message", {}).get("id")
            if asst_id not in seen_asst_ids:
                flush()
                seen_asst_ids.add(asst_id)
    flush()

    return groups


def _partition_by_prior_decision(
    candidates: list[_ToolResultCandidate],
    state: ContentReplacementState,
) -> _CandidatePartition:
    """Partition candidates by their prior decision state (must-reapply / frozen / fresh)."""
    partition = _CandidatePartition()
    for c in candidates:
        replacement = state.replacements.get(c.tool_use_id)
        if replacement is not None:
            partition.must_reapply.append((c, replacement))
        elif c.tool_use_id in state.seen_ids:
            partition.frozen.append(c)
        else:
            partition.fresh.append(c)
    return partition


def _select_fresh_to_replace(
    fresh: list[_ToolResultCandidate],
    frozen_size: int,
    limit: int,
) -> list[_ToolResultCandidate]:
    """Pick the largest fresh results to replace until total is at/under budget."""
    sorted_fresh = sorted(fresh, key=lambda c: c.size, reverse=True)
    selected: list[_ToolResultCandidate] = []
    remaining = frozen_size + sum(c.size for c in fresh)
    for c in sorted_fresh:
        if remaining <= limit:
            break
        selected.append(c)
        remaining -= c.size
    return selected


def _replace_tool_result_contents(
    messages: list[dict[str, Any]],
    replacement_map: dict[str, str],
) -> list[dict[str, Any]]:
    """Return a new message list where each replaced tool_result block has new content.

    Messages and blocks with no replacements are passed through by reference.
    """
    result: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("message", {}).get("content")
        if message.get("type") != "user" or not isinstance(content, list):
            result.append(message)
            continue
        needs_replace = any(
            b.get("type") == "tool_result" and b.get("tool_use_id") in replacement_map
            for b in content
        )
        if not needs_replace:
            result.append(message)
            continue
        new_content: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") != "tool_result":
                new_content.append(block)
                continue
            replacement = replacement_map.get(block.get("tool_use_id"))
            new_content.append(
                block if replacement is None else {**block, "content": replacement}
            )
        result.append(
            {
                **message,
                "message": {**message["message"], "content": new_content},
            }
        )
    return result


async def _build_replacement(
    candidate: _ToolResultCandidate,
) -> dict[str, Any] | None:
    result = await persist_tool_result(candidate.content, candidate.tool_use_id)
    if is_persist_error(result):
        return None
    return {
        "content": build_large_tool_result_message(result),
        "originalSize": result["originalSize"],
    }


async def enforce_tool_result_budget(
    messages: list[dict[str, Any]],
    state: ContentReplacementState,
    skip_tool_names: AbstractSet[str] | None = None,
) -> dict[str, Any]:
    """Enforce the per-message budget on aggregate tool result size.

    ``state`` is MUTATED in place. Returns ``{'messages', 'newlyReplaced'}``: ``messages`` is the
    same instance when no replacement was needed; ``newlyReplaced`` are the replacements made
    THIS call (not re-applies), as ``ToolResultReplacementRecord`` dicts.
    """
    if skip_tool_names is None:
        skip_tool_names = set()
    candidates_by_message = _collect_candidates_by_message(messages)
    name_by_tool_use_id = (
        _build_tool_name_map(messages) if len(skip_tool_names) > 0 else None
    )

    def should_skip(tool_use_id: str) -> bool:
        return (
            name_by_tool_use_id is not None
            and name_by_tool_use_id.get(tool_use_id, "") in skip_tool_names
        )

    limit = get_per_message_budget_limit()

    replacement_map: dict[str, str] = {}
    to_persist: list[_ToolResultCandidate] = []
    reapplied_count = 0
    messages_over_budget = 0

    for candidates in candidates_by_message:
        partition = _partition_by_prior_decision(candidates, state)

        for c, replacement in partition.must_reapply:
            replacement_map[c.tool_use_id] = replacement
        reapplied_count += len(partition.must_reapply)

        if len(partition.fresh) == 0:
            for c in candidates:
                state.seen_ids.add(c.tool_use_id)
            continue

        # Tools with max_result_size_chars Infinity (Read) — never persist; mark seen (frozen).
        skipped = [c for c in partition.fresh if should_skip(c.tool_use_id)]
        for c in skipped:
            state.seen_ids.add(c.tool_use_id)
        eligible = [c for c in partition.fresh if not should_skip(c.tool_use_id)]

        frozen_size = sum(c.size for c in partition.frozen)
        fresh_size = sum(c.size for c in eligible)

        selected = (
            _select_fresh_to_replace(eligible, frozen_size, limit)
            if frozen_size + fresh_size > limit
            else []
        )

        selected_ids = {c.tool_use_id for c in selected}
        for c in candidates:
            if c.tool_use_id not in selected_ids:
                state.seen_ids.add(c.tool_use_id)

        if len(selected) == 0:
            continue
        messages_over_budget += 1
        to_persist.extend(selected)

    if len(replacement_map) == 0 and len(to_persist) == 0:
        return {"messages": messages, "newlyReplaced": []}

    fresh_replacements = await asyncio.gather(
        *(_build_replacement(c) for c in to_persist)
    )
    newly_replaced: list[ToolResultReplacementRecord] = []
    replaced_size = 0
    for candidate, replacement in zip(to_persist, fresh_replacements, strict=True):
        state.seen_ids.add(candidate.tool_use_id)
        if replacement is None:
            continue
        replaced_size += candidate.size
        replacement_map[candidate.tool_use_id] = replacement["content"]
        state.replacements[candidate.tool_use_id] = replacement["content"]
        newly_replaced.append(
            {
                "kind": "tool-result",
                "toolUseId": candidate.tool_use_id,
                "replacement": replacement["content"],
            }
        )

    if len(replacement_map) == 0:
        return {"messages": messages, "newlyReplaced": []}

    if len(newly_replaced) > 0:
        log_for_debugging(
            f"Per-message budget: persisted {len(newly_replaced)} tool results "
            f"across {messages_over_budget} over-budget message(s), "
            f"shed ~{format_file_size(replaced_size)}, {reapplied_count} re-applied"
        )

    return {
        "messages": _replace_tool_result_contents(messages, replacement_map),
        "newlyReplaced": newly_replaced,
    }


async def apply_tool_result_budget(
    messages: list[dict[str, Any]],
    state: ContentReplacementState | None,
    write_to_transcript: Any = None,
    skip_tool_names: AbstractSet[str] | None = None,
) -> list[dict[str, Any]]:
    """Query-loop integration point for the aggregate budget (gates on ``state``)."""
    if not state:
        return messages
    result = await enforce_tool_result_budget(messages, state, skip_tool_names)
    if len(result["newlyReplaced"]) > 0 and write_to_transcript is not None:
        write_to_transcript(result["newlyReplaced"])
    return result["messages"]


def reconstruct_content_replacement_state(
    messages: list[dict[str, Any]],
    records: list[ContentReplacementRecord],
    inherited_replacements: Mapping[str, str] | None = None,
) -> ContentReplacementState:
    """Reconstruct replacement state from content-replacement records loaded from the transcript."""
    state = create_content_replacement_state()
    candidate_ids = {
        c.tool_use_id
        for group in _collect_candidates_by_message(messages)
        for c in group
    }

    for tool_use_id in candidate_ids:
        state.seen_ids.add(tool_use_id)
    for r in records:
        if r.get("kind") == "tool-result" and r.get("toolUseId") in candidate_ids:
            state.replacements[r["toolUseId"]] = r["replacement"]
    if inherited_replacements:
        for tool_use_id, replacement in inherited_replacements.items():
            if (
                tool_use_id in candidate_ids
                and tool_use_id not in state.replacements
            ):
                state.replacements[tool_use_id] = replacement
    return state


def reconstruct_for_subagent_resume(
    parent_state: ContentReplacementState | None,
    resumed_messages: list[dict[str, Any]],
    sidechain_records: list[ContentReplacementRecord],
) -> ContentReplacementState | None:
    """AgentTool-resume variant: feature-flag gate + parent gap-fill."""
    if not parent_state:
        return None
    return reconstruct_content_replacement_state(
        resumed_messages, sidechain_records, parent_state.replacements
    )


def _get_file_system_error_message(error: OSError) -> str:
    """Get a human-readable error message from a filesystem error."""
    code = get_errno_code(error)
    path = getattr(error, "filename", None) or "unknown path"
    if code:
        if code == "ENOENT":
            return f"Directory not found: {path}"
        if code == "EACCES":
            return f"Permission denied: {path}"
        if code == "ENOSPC":
            return "No space left on device"
        if code == "EROFS":
            return "Read-only file system"
        if code == "EMFILE":
            return "Too many open files"
        if code == "EEXIST":
            return f"File already exists: {path}"
        return f"{code}: {error.strerror or str(error)}"
    return str(error)
