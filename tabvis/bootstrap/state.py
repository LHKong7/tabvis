"""Session / cost / duration / stats global-state hub

A DAG-leaf module of process-global state for the active Tabvis session: the session id and
lineage, the original cwd / project root / live cwd, the cost / duration / tool-duration /
lines-changed / token accumulators, per-model usage, the stats store, telemetry counter
handles, and a grab-bag of session-only flags. Mirrors the TS module exactly: a single
module-level ``_STATE`` object built by :func:`_get_initial_state`, plus snake_case
getter/setter functions.

Casing rule (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; dict-shaped
data that round-trips to the API / transcript / SDK keeps its wire keys. Here the only such
dict is per-model ``ModelUsage`` (camelCase token fields ``inputTokens`` / ``outputTokens`` /
``cacheReadInputTokens`` / ``cacheCreationInputTokens`` / ``webSearchRequests``) — those keys
are preserved verbatim.

Subscribe surface: the ``onSessionSwitch`` store is the existing
:func:`tabvis.utils.signal.create_signal` (a pure event signal), exactly as
``tabvis/utils/query_guard.py`` uses it. ``on_session_switch`` is bound to the signal's
``subscribe`` so it stays a stable reference (matching the TS
``export const onSessionSwitch = sessionSwitched.subscribe``).

Faithful-behavior notes:
- ``_get_initial_state`` resolves cwd via ``os.path.realpath`` + NFC normalization with an
  EPERM fallback to the raw cwd (mirrors ``realpathSync(cwd()).normalize('NFC')``). In a clean
  env this equals ``tabvis.utils.cwd.get_original_cwd()`` (both yield ``os.getcwd()``), so the
  ``--dump-system-prompt`` golden is unaffected — nothing imports this module yet.
- The telemetry / OTel counter handles, logger/meter/tracer providers, and ``BetaMessageStreamParams``
  last-request payloads are opaque values typed ``Any`` (those subsystems aren't implemented);
  they default to ``None`` and round-trip unchanged.
- ``ModelSetting`` / ``ModelStrings`` are not implemented in this build; typed ``Any`` here.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from time import time
from typing import Any, Literal, TypedDict

from tabvis.types.ids import SessionId
from tabvis.utils.crypto import random_uuid
from tabvis.utils.settings.constants import SettingSource
from tabvis.utils.signal import create_signal

# DO NOT ADD MORE STATE HERE - BE JUDICIOUS WITH GLOBAL STATE

# ``ModelSetting`` (src/utils/model/model.ts) and ``ModelStrings``
# (src/utils/model/modelStrings.ts) are not yet implemented. Typed ``Any`` as opaque values.
ModelSetting = Any
ModelStrings = Any

# ``ModelUsage`` (src/entrypoints/agentSdkTypes.ts) is a plain wire dict with camelCase token
# fields. Kept as a TypedDict so the token-sum accessors below can read those exact wire keys.
ModelUsage = dict[str, Any]


def _now_ms() -> int:
    """``Date.now()`` — milliseconds since the Unix epoch as an int."""
    return int(time() * 1000)


class AttributedCounter:
    """Structural type for an OTel attributed counter (``add(value, attrs?)``).

    A marker base; real counters come from the telemetry factory passed to :func:`set_meter`.
    """

    def add(self, value: float, additional_attributes: Any | None = None) -> None:  # noqa: D102
        raise NotImplementedError


class StatsStore(TypedDict):
    """The minimal stats-store surface used here: ``observe(name, value)``."""

    observe: Any


class TeleportedSessionInfo(TypedDict):
    isTeleported: bool
    hasLoggedFirstMessage: bool
    sessionId: str | None


class InvokedSkillInfo(TypedDict):
    skillName: str
    skillPath: str
    content: str
    invokedAt: int
    agentId: str | None


class SlowOperation(TypedDict):
    operation: str
    durationMs: int
    timestamp: int


class ErrorLogEntry(TypedDict):
    error: str
    timestamp: str


@dataclass
class State:
    """Process-global session state. Mirrors the TS ``State`` type field-for-field."""

    original_cwd: str
    # Stable project root - set once at startup (including by --worktree flag),
    # never updated by mid-session EnterWorktreeTool.
    # Use for project identity (history, skills, sessions) not file operations.
    project_root: str
    total_cost_usd: float
    total_api_duration: float
    total_api_duration_without_retries: float
    total_tool_duration: float
    turn_hook_duration_ms: float
    turn_tool_duration_ms: float
    turn_tool_count: int
    turn_hook_count: int
    start_time: int
    last_interaction_time: int
    total_lines_added: int
    total_lines_removed: int
    has_unknown_model_cost: bool
    cwd: str
    model_usage: dict[str, ModelUsage]
    main_loop_model_override: ModelSetting | None
    initial_main_loop_model: ModelSetting
    model_strings: ModelStrings | None
    is_interactive: bool
    # When true, ensureToolResultPairing throws on mismatch instead of
    # repairing with synthetic placeholders.
    strict_tool_result_pairing: bool
    sdk_agent_progress_summaries_enabled: bool
    client_type: str
    session_source: str | None
    question_preview_format: Literal["markdown", "html"] | None
    flag_settings_path: str | None
    flag_settings_inline: dict[str, Any] | None
    allowed_setting_sources: list[SettingSource]
    session_ingress_token: str | None
    access_token_from_fd: str | None
    api_key_from_fd: str | None
    # Telemetry state
    meter: Any | None
    session_counter: AttributedCounter | None
    loc_counter: AttributedCounter | None
    pr_counter: AttributedCounter | None
    commit_counter: AttributedCounter | None
    cost_counter: AttributedCounter | None
    token_counter: AttributedCounter | None
    code_edit_tool_decision_counter: AttributedCounter | None
    active_time_counter: AttributedCounter | None
    stats_store: Any | None
    session_id: SessionId
    # Parent session ID for tracking session lineage (e.g., plan mode -> implementation)
    parent_session_id: SessionId | None
    # Logger state
    logger_provider: Any | None
    event_logger: Any | None
    # Meter provider state
    meter_provider: Any | None
    # Tracer provider state
    tracer_provider: Any | None
    # Agent color state
    agent_color_map: dict[str, Any]
    agent_color_index: int
    # Last API request for bug reports (Omit<BetaMessageStreamParams, 'messages'>)
    last_api_request: Any | None
    # Messages from the last API request (tabvis-only; reference, not clone).
    last_api_request_messages: Any | None
    # In-memory error log for recent errors
    in_memory_error_log: list[ErrorLogEntry]
    # Session-only bypass permissions mode flag (not persisted)
    session_bypass_permissions_mode: bool
    # Teams created this session via TeamCreate.
    session_created_teams: set[str]
    # Session-only trust flag for home directory (not persisted to disk)
    session_trust_accepted: bool
    # Session-only flag to disable session persistence to disk
    session_persistence_disabled: bool
    # Track if user has exited plan mode in this session (for re-entry guidance)
    has_exited_plan_mode: bool
    # Track if we need to show the plan mode exit attachment (one-time notification)
    needs_plan_mode_exit_attachment: bool
    # SDK init event state - jsonSchema for structured output
    init_json_schema: dict[str, Any] | None
    # Registered hooks - SDK callbacks (Partial<Record<HookEvent, RegisteredHookMatcher[]>>)
    registered_hooks: dict[str, list[Any]] | None
    # Cache for plan slugs: sessionId -> wordSlug
    plan_slug_cache: dict[str, str]
    # Track teleported session for reliability logging
    teleported_session_info: TeleportedSessionInfo | None
    # Track invoked skills for preservation across compaction. Keys are composite:
    # `${agentId ?? ''}:${skillName}` to prevent cross-agent overwrites.
    invoked_skills: dict[str, InvokedSkillInfo]
    # Track slow operations for dev bar display (tabvis-only)
    slow_operations: list[SlowOperation]
    # SDK-provided betas (e.g., context-1m-2025-08-07)
    sdk_betas: list[str] | None
    # Main thread agent type (from --agent flag or settings)
    main_thread_agent_type: str | None
    # Remote mode (--remote flag)
    is_remote_mode: bool
    # Direct connect server URL (for display in header)
    direct_connect_server_url: str | None
    # System prompt section cache state
    system_prompt_section_cache: dict[str, str | None]
    # Last date emitted to the model (for detecting midnight date changes)
    last_emitted_date: str | None
    # Additional directories from --add-dir flag (for TABVIS.md loading)
    additional_directories_for_tabvis_md: list[str]
    # Dir containing the session's `.jsonl`; null = derive from originalCwd.
    session_project_dir: str | None
    # Cached prompt cache 1h TTL allowlist from GrowthBook (session-stable)
    prompt_cache_1h_allowlist: list[str] | None
    # Cached 1h TTL user eligibility (session-stable).
    prompt_cache_1h_eligible: bool | None
    # Sticky-on latch for the cache-editing beta header.
    cache_editing_header_latched: bool | None
    # Sticky-on latch for clearing thinking from prior tool loops.
    thinking_clear_latched: bool | None
    # Current prompt ID (UUID) correlating a user prompt with subsequent OTel events
    prompt_id: str | None
    # Last API requestId for the main conversation chain (not subagents).
    last_main_request_id: str | None
    # Timestamp (Date.now()) of the last successful API call completion.
    last_api_completion_timestamp: int | None
    # Set to true after compaction (auto or manual /compact).
    pending_post_compaction: bool


# ALSO HERE - THINK THRICE BEFORE MODIFYING
def _get_initial_state() -> State:
    # Resolve symlinks in cwd to match behavior of shell.ts setCwd. This ensures
    # consistency with how paths are sanitized for session storage.
    resolved_cwd = ""
    raw_cwd = ""
    try:
        raw_cwd = os.getcwd()
    except OSError:
        raw_cwd = ""
    if raw_cwd:
        try:
            resolved_cwd = unicodedata.normalize("NFC", os.path.realpath(raw_cwd))
        except OSError:
            # File Provider EPERM on CloudStorage mounts (lstat per path component).
            resolved_cwd = unicodedata.normalize("NFC", raw_cwd)

    now = _now_ms()
    return State(
        original_cwd=resolved_cwd,
        project_root=resolved_cwd,
        total_cost_usd=0,
        total_api_duration=0,
        total_api_duration_without_retries=0,
        total_tool_duration=0,
        turn_hook_duration_ms=0,
        turn_tool_duration_ms=0,
        turn_tool_count=0,
        turn_hook_count=0,
        start_time=now,
        last_interaction_time=now,
        total_lines_added=0,
        total_lines_removed=0,
        has_unknown_model_cost=False,
        cwd=resolved_cwd,
        model_usage={},
        main_loop_model_override=None,
        initial_main_loop_model=None,
        model_strings=None,
        is_interactive=False,
        strict_tool_result_pairing=False,
        sdk_agent_progress_summaries_enabled=False,
        client_type="cli",
        session_source=None,
        question_preview_format=None,
        session_ingress_token=None,
        access_token_from_fd=None,
        api_key_from_fd=None,
        flag_settings_path=None,
        flag_settings_inline=None,
        allowed_setting_sources=[
            "userSettings",
            "projectSettings",
            "localSettings",
            "flagSettings",
            "policySettings",
        ],
        # Telemetry state
        meter=None,
        session_counter=None,
        loc_counter=None,
        pr_counter=None,
        commit_counter=None,
        cost_counter=None,
        token_counter=None,
        code_edit_tool_decision_counter=None,
        active_time_counter=None,
        stats_store=None,
        session_id=SessionId(random_uuid()),
        parent_session_id=None,
        # Logger state
        logger_provider=None,
        event_logger=None,
        # Meter provider state
        meter_provider=None,
        tracer_provider=None,
        # Agent color state
        agent_color_map={},
        agent_color_index=0,
        # Last API request for bug reports
        last_api_request=None,
        last_api_request_messages=None,
        # In-memory error log for recent errors
        in_memory_error_log=[],
        # Session-only bypass permissions mode flag (not persisted)
        session_bypass_permissions_mode=False,
        session_created_teams=set(),
        # Session-only trust flag (not persisted to disk)
        session_trust_accepted=False,
        # Session-only flag to disable session persistence to disk
        session_persistence_disabled=False,
        # Track if user has exited plan mode in this session
        has_exited_plan_mode=False,
        # Track if we need to show the plan mode exit attachment
        needs_plan_mode_exit_attachment=False,
        # SDK init event state
        init_json_schema=None,
        registered_hooks=None,
        # Cache for plan slugs
        plan_slug_cache={},
        # Track teleported session for reliability logging
        teleported_session_info=None,
        # Track invoked skills for preservation across compaction
        invoked_skills={},
        # Track slow operations for dev bar display
        slow_operations=[],
        # SDK-provided betas
        sdk_betas=None,
        # Main thread agent type
        main_thread_agent_type=None,
        # Remote mode
        is_remote_mode=False,
        # Direct connect server URL
        direct_connect_server_url=None,
        # System prompt section cache state
        system_prompt_section_cache={},
        # Last date emitted to the model
        last_emitted_date=None,
        # Additional directories from --add-dir flag (for TABVIS.md loading)
        additional_directories_for_tabvis_md=[],
        # Session project dir (null = derive from originalCwd)
        session_project_dir=None,
        # Prompt cache 1h allowlist (null = not yet fetched from GrowthBook)
        prompt_cache_1h_allowlist=None,
        # Prompt cache 1h eligibility (null = not yet evaluated)
        prompt_cache_1h_eligible=None,
        # Beta header latches (null = not yet triggered)
        cache_editing_header_latched=None,
        thinking_clear_latched=None,
        # Current prompt ID
        prompt_id=None,
        last_main_request_id=None,
        last_api_completion_timestamp=None,
        pending_post_compaction=False,
    )


# AND ESPECIALLY HERE
_STATE: State = _get_initial_state()


# --
# Session id / lineage


def get_session_id() -> SessionId:
    return _STATE.session_id


def regenerate_session_id(set_current_as_parent: bool = False) -> SessionId:
    if set_current_as_parent:
        _STATE.parent_session_id = _STATE.session_id
    # Drop the outgoing session's plan-slug entry so the map doesn't accumulate stale keys.
    _STATE.plan_slug_cache.pop(_STATE.session_id, None)
    # Regenerated sessions live in the current project: reset projectDir to null so
    # getTranscriptPath() derives from originalCwd.
    _STATE.session_id = SessionId(random_uuid())
    _STATE.session_project_dir = None
    return _STATE.session_id


def get_parent_session_id() -> SessionId | None:
    return _STATE.parent_session_id


_session_switched = create_signal()


def switch_session(session_id: SessionId, project_dir: str | None = None) -> None:
    """Atomically switch the active session.

    ``session_id`` and ``session_project_dir`` always change together (CC-34). ``project_dir``
    is the directory containing ``<sessionId>.jsonl``; omit (or pass ``None``) for sessions in
    the current project — the path derives from originalCwd at read time. Every call resets the
    project dir; it never carries over from the previous session.
    """
    # Drop the outgoing session's plan-slug entry so the map stays bounded across repeated
    # /resume. Only the current session's slug is ever read.
    _STATE.plan_slug_cache.pop(_STATE.session_id, None)
    _STATE.session_id = session_id
    _STATE.session_project_dir = project_dir
    _session_switched.emit(session_id)


# Register a callback that fires when switch_session changes the active sessionId. Bound to the
# signal's subscribe so it stays a stable reference (TS: `export const onSessionSwitch =
# sessionSwitched.subscribe`).
on_session_switch = _session_switched.subscribe


def get_session_project_dir() -> str | None:
    """Project directory the current session's transcript lives in, or ``None`` if the session
    was created in the current project (derive from originalCwd). See :func:`switch_session`."""
    return _STATE.session_project_dir


# --
# cwd / project root


def get_original_cwd() -> str:
    return _STATE.original_cwd


def get_project_root() -> str:
    """Get the stable project root directory.

    Unlike :func:`get_original_cwd`, this is never updated by mid-session EnterWorktreeTool (so
    skills/history stay stable when entering a throwaway worktree). It IS set at startup by
    --worktree, since that worktree is the session's project. Use for project identity
    (history, skills, sessions) not file operations.
    """
    return _STATE.project_root


def set_original_cwd(cwd: str) -> None:
    _STATE.original_cwd = unicodedata.normalize("NFC", cwd)


def set_project_root(cwd: str) -> None:
    """Only for --worktree startup flag. Mid-session EnterWorktreeTool must NOT call this —
    skills/history should stay anchored to where the session started."""
    _STATE.project_root = unicodedata.normalize("NFC", cwd)


def get_cwd_state() -> str:
    return _STATE.cwd


def set_cwd_state(cwd: str) -> None:
    _STATE.cwd = unicodedata.normalize("NFC", cwd)


def get_direct_connect_server_url() -> str | None:
    return _STATE.direct_connect_server_url


def set_direct_connect_server_url(url: str) -> None:
    _STATE.direct_connect_server_url = url


# --
# Duration accumulators


def add_to_total_duration_state(duration: float, duration_without_retries: float) -> None:
    _STATE.total_api_duration += duration
    _STATE.total_api_duration_without_retries += duration_without_retries


def reset_total_duration_state_and_cost_for_tests_only() -> None:  # noqa: N802 (TS _FOR_TESTS_ONLY marker)
    _STATE.total_api_duration = 0
    _STATE.total_api_duration_without_retries = 0
    _STATE.total_cost_usd = 0


# --
# Cost accumulators


def add_to_total_cost_state(cost: float, model_usage: ModelUsage, model: str) -> None:
    _STATE.model_usage[model] = model_usage
    _STATE.total_cost_usd += cost


def get_total_cost_usd() -> float:
    return _STATE.total_cost_usd


def get_total_api_duration() -> float:
    return _STATE.total_api_duration


def get_total_duration() -> int:
    return _now_ms() - _STATE.start_time


def get_total_api_duration_without_retries() -> float:
    return _STATE.total_api_duration_without_retries


def get_total_tool_duration() -> float:
    return _STATE.total_tool_duration


def add_to_tool_duration(duration: float) -> None:
    _STATE.total_tool_duration += duration
    _STATE.turn_tool_duration_ms += duration
    _STATE.turn_tool_count += 1


def get_turn_hook_duration_ms() -> float:
    return _STATE.turn_hook_duration_ms


def add_to_turn_hook_duration(duration: float) -> None:
    _STATE.turn_hook_duration_ms += duration
    _STATE.turn_hook_count += 1


def reset_turn_hook_duration() -> None:
    _STATE.turn_hook_duration_ms = 0
    _STATE.turn_hook_count = 0


def get_turn_hook_count() -> int:
    return _STATE.turn_hook_count


def get_turn_tool_duration_ms() -> float:
    return _STATE.turn_tool_duration_ms


def reset_turn_tool_duration() -> None:
    _STATE.turn_tool_duration_ms = 0
    _STATE.turn_tool_count = 0


def get_turn_tool_count() -> int:
    return _STATE.turn_tool_count


# --
# Stats store


def get_stats_store() -> Any | None:
    return _STATE.stats_store


def set_stats_store(store: Any | None) -> None:
    _STATE.stats_store = store


# --
# Interaction time
#
# By default the actual Date.now() call is deferred until the next render frame (via
# flush_interaction_time) so we avoid calling Date.now() on every single keypress. Pass
# immediate=True when calling from code that runs *after* the render cycle has already flushed.

_interaction_time_dirty = False


def update_last_interaction_time(immediate: bool = False) -> None:
    global _interaction_time_dirty
    if immediate:
        _flush_interaction_time_inner()
    else:
        _interaction_time_dirty = True


def flush_interaction_time() -> None:
    """If an interaction was recorded since the last flush, update the timestamp now. Called
    before each render cycle so we batch many keypresses into a single Date.now() call."""
    if _interaction_time_dirty:
        _flush_interaction_time_inner()


def _flush_interaction_time_inner() -> None:
    global _interaction_time_dirty
    _STATE.last_interaction_time = _now_ms()
    _interaction_time_dirty = False


# --
# Lines changed


def add_to_total_lines_changed(added: int, removed: int) -> None:
    _STATE.total_lines_added += added
    _STATE.total_lines_removed += removed


def get_total_lines_added() -> int:
    return _STATE.total_lines_added


def get_total_lines_removed() -> int:
    return _STATE.total_lines_removed


# --
# Token accumulators (sum over per-model ModelUsage wire dicts)


def _sum_by(field_name: str) -> int:
    return sum(usage.get(field_name, 0) or 0 for usage in _STATE.model_usage.values())


def get_total_input_tokens() -> int:
    return _sum_by("inputTokens")


def get_total_output_tokens() -> int:
    return _sum_by("outputTokens")


def get_total_cache_read_input_tokens() -> int:
    return _sum_by("cacheReadInputTokens")


def get_total_cache_creation_input_tokens() -> int:
    return _sum_by("cacheCreationInputTokens")


def get_total_web_search_requests() -> int:
    return _sum_by("webSearchRequests")


_output_tokens_at_turn_start = 0
_current_turn_token_budget: int | None = None
_budget_continuation_count = 0


def get_turn_output_tokens() -> int:
    return get_total_output_tokens() - _output_tokens_at_turn_start


def get_current_turn_token_budget() -> int | None:
    return _current_turn_token_budget


def snapshot_output_tokens_for_turn(budget: int | None) -> None:
    global _output_tokens_at_turn_start, _current_turn_token_budget, _budget_continuation_count
    _output_tokens_at_turn_start = get_total_output_tokens()
    _current_turn_token_budget = budget
    _budget_continuation_count = 0


def get_budget_continuation_count() -> int:
    return _budget_continuation_count


def increment_budget_continuation_count() -> None:
    global _budget_continuation_count
    _budget_continuation_count += 1


# --
# Unknown model cost


def set_has_unknown_model_cost() -> None:
    _STATE.has_unknown_model_cost = True


def has_unknown_model_cost() -> bool:
    return _STATE.has_unknown_model_cost


# --
# Last main request id / api completion / post-compaction


def get_last_main_request_id() -> str | None:
    return _STATE.last_main_request_id


def set_last_main_request_id(request_id: str) -> None:
    _STATE.last_main_request_id = request_id


def get_last_api_completion_timestamp() -> int | None:
    return _STATE.last_api_completion_timestamp


def set_last_api_completion_timestamp(timestamp: int) -> None:
    _STATE.last_api_completion_timestamp = timestamp


def mark_post_compaction() -> None:
    """Mark that a compaction just occurred. The next API success event will include
    isPostCompaction=true, then the flag auto-resets."""
    _STATE.pending_post_compaction = True


def consume_post_compaction() -> bool:
    """Consume the post-compaction flag. Returns true once after compaction, then false until
    the next compaction."""
    was = _STATE.pending_post_compaction
    _STATE.pending_post_compaction = False
    return was


def get_last_interaction_time() -> int:
    return _STATE.last_interaction_time


# --
# Scroll drain suspension
#
# Module-scope (not in STATE) — ephemeral hot-path flag, no test-reset needed since the
# debounce timer self-clears. The TS ``setTimeout`` debounce isn't implemented here (no Ink render
# loop in the headless path); ``mark_scroll_activity`` sets the flag, and there is no timer to
# the 150ms debounce when the terminal render loop lands.

_scroll_draining = False
_SCROLL_DRAIN_IDLE_MS = 0.150


def mark_scroll_activity() -> None:
    """Mark that a scroll event just happened. Background intervals gate on
    :func:`get_is_scroll_draining` and skip their work until the debounce clears."""
    global _scroll_draining
    _scroll_draining = True


def get_is_scroll_draining() -> bool:
    """True while scroll is actively draining. Intervals should early-return when this is set."""
    return _scroll_draining


async def wait_for_scroll_idle() -> None:
    """Await this before expensive one-shot work that could coincide with scroll. Resolves
    immediately if not scrolling; otherwise polls at the idle interval until the flag clears."""
    import asyncio

    while _scroll_draining:
        await asyncio.sleep(_SCROLL_DRAIN_IDLE_MS)


# --
# Model usage / model setting / model strings


def get_model_usage() -> dict[str, ModelUsage]:
    return _STATE.model_usage


def get_usage_for_model(model: str) -> ModelUsage | None:
    return _STATE.model_usage.get(model)


def get_main_loop_model_override() -> ModelSetting | None:
    """Gets the model override set from the --model CLI flag or after the user updates their
    configured model."""
    return _STATE.main_loop_model_override


def get_initial_main_loop_model() -> ModelSetting:
    return _STATE.initial_main_loop_model


def set_main_loop_model_override(model: ModelSetting | None) -> None:
    _STATE.main_loop_model_override = model


def set_initial_main_loop_model(model: ModelSetting) -> None:
    _STATE.initial_main_loop_model = model


def get_sdk_betas() -> list[str] | None:
    return _STATE.sdk_betas


def set_sdk_betas(betas: list[str] | None) -> None:
    _STATE.sdk_betas = betas


# --
# Cost-state reset / restore


def reset_cost_state() -> None:
    _STATE.total_cost_usd = 0
    _STATE.total_api_duration = 0
    _STATE.total_api_duration_without_retries = 0
    _STATE.total_tool_duration = 0
    _STATE.start_time = _now_ms()
    _STATE.total_lines_added = 0
    _STATE.total_lines_removed = 0
    _STATE.has_unknown_model_cost = False
    _STATE.model_usage = {}
    _STATE.prompt_id = None


def set_cost_state_for_restore(
    total_cost_usd: float,
    total_api_duration: float,
    total_api_duration_without_retries: float,
    total_tool_duration: float,
    total_lines_added: int,
    total_lines_removed: int,
    last_duration: int | None,
    model_usage: dict[str, ModelUsage] | None,
) -> None:
    """Sets cost state values for session restore. Called by restoreCostStateForSession in
    cost-tracker.ts."""
    _STATE.total_cost_usd = total_cost_usd
    _STATE.total_api_duration = total_api_duration
    _STATE.total_api_duration_without_retries = total_api_duration_without_retries
    _STATE.total_tool_duration = total_tool_duration
    _STATE.total_lines_added = total_lines_added
    _STATE.total_lines_removed = total_lines_removed

    # Restore per-model usage breakdown
    if model_usage:
        _STATE.model_usage = model_usage

    # Adjust startTime to make wall duration accumulate
    if last_duration:
        _STATE.start_time = _now_ms() - last_duration


# --
# Test-only reset


def reset_state_for_tests() -> None:
    """Reset all module-level state to its initial values. Only for tests.

    Intentional divergence from resetStateForTests (state.ts:813): the TS raises when
    ``process.env.NODE_ENV !== 'test'`` as a footgun-rail. We omit that guard — it is test-only
    (no runtime/wire impact; the function is never called outside tests), and a faithful
    ``NODE_ENV``-keyed guard would collide with this suite's own threshold tests, which
    legitimately set ``NODE_ENV=development``/``test`` (see tests/test_wave5c_fs_chain.py).
    """
    global _STATE, _output_tokens_at_turn_start, _current_turn_token_budget
    global _budget_continuation_count, _interaction_time_dirty, _scroll_draining
    _STATE = _get_initial_state()
    _output_tokens_at_turn_start = 0
    _current_turn_token_budget = None
    _budget_continuation_count = 0
    _interaction_time_dirty = False
    _scroll_draining = False
    _session_switched.clear()


# --
# Model strings (use src/utils/model/modelStrings.ts::get_model_strings() instead)


def get_model_strings() -> ModelStrings | None:
    return _STATE.model_strings


def set_model_strings(model_strings: ModelStrings) -> None:
    _STATE.model_strings = model_strings


def reset_model_strings_for_testing_only() -> None:
    """Test utility to reset model strings for re-initialization (accepts only None)."""
    _STATE.model_strings = None


# --
# Telemetry meter + counters


def set_meter(meter: Any, create_counter: Any) -> None:
    _STATE.meter = meter

    # Initialize all counters using the provided factory.
    _STATE.session_counter = create_counter(
        "tabvis.session.count",
        {"description": "Count of CLI sessions started"},
    )
    _STATE.loc_counter = create_counter(
        "tabvis.lines_of_code.count",
        {
            "description": (
                "Count of lines of code modified, with the 'type' attribute indicating "
                "whether lines were added or removed"
            )
        },
    )
    _STATE.pr_counter = create_counter(
        "tabvis.pull_request.count",
        {"description": "Number of pull requests created"},
    )
    _STATE.commit_counter = create_counter(
        "tabvis.commit.count",
        {"description": "Number of git commits created"},
    )
    _STATE.cost_counter = create_counter(
        "tabvis.cost.usage",
        {"description": "Cost of the Tabvis session", "unit": "USD"},
    )
    _STATE.token_counter = create_counter(
        "tabvis.token.usage",
        {"description": "Number of tokens used", "unit": "tokens"},
    )
    _STATE.code_edit_tool_decision_counter = create_counter(
        "tabvis.code_edit_tool.decision",
        {
            "description": (
                "Count of code editing tool permission decisions (accept/reject) for Edit, "
                "Write, and NotebookEdit tools"
            )
        },
    )
    _STATE.active_time_counter = create_counter(
        "tabvis.active_time.total",
        {"description": "Total active time in seconds", "unit": "s"},
    )


def get_meter() -> Any | None:
    return _STATE.meter


def get_session_counter() -> AttributedCounter | None:
    return _STATE.session_counter


def get_loc_counter() -> AttributedCounter | None:
    return _STATE.loc_counter


def get_pr_counter() -> AttributedCounter | None:
    return _STATE.pr_counter


def get_commit_counter() -> AttributedCounter | None:
    return _STATE.commit_counter


def get_cost_counter() -> AttributedCounter | None:
    return _STATE.cost_counter


def get_token_counter() -> AttributedCounter | None:
    return _STATE.token_counter


def get_code_edit_tool_decision_counter() -> AttributedCounter | None:
    return _STATE.code_edit_tool_decision_counter


def get_active_time_counter() -> AttributedCounter | None:
    return _STATE.active_time_counter


# --
# Logger / meter / tracer providers


def get_logger_provider() -> Any | None:
    return _STATE.logger_provider


def set_logger_provider(provider: Any | None) -> None:
    _STATE.logger_provider = provider


def get_event_logger() -> Any | None:
    return _STATE.event_logger


def set_event_logger(logger: Any | None) -> None:
    _STATE.event_logger = logger


def get_meter_provider() -> Any | None:
    return _STATE.meter_provider


def set_meter_provider(provider: Any | None) -> None:
    _STATE.meter_provider = provider


def get_tracer_provider() -> Any | None:
    return _STATE.tracer_provider


def set_tracer_provider(provider: Any | None) -> None:
    _STATE.tracer_provider = provider


# --
# Interactivity / client / session flags


def get_is_non_interactive_session() -> bool:
    return not _STATE.is_interactive


def get_is_interactive() -> bool:
    return _STATE.is_interactive


def set_is_interactive(value: bool) -> None:
    _STATE.is_interactive = value


def get_client_type() -> str:
    return _STATE.client_type


def set_client_type(client_type: str) -> None:
    _STATE.client_type = client_type


def get_sdk_agent_progress_summaries_enabled() -> bool:
    return _STATE.sdk_agent_progress_summaries_enabled


def set_sdk_agent_progress_summaries_enabled(value: bool) -> None:
    _STATE.sdk_agent_progress_summaries_enabled = value


def get_strict_tool_result_pairing() -> bool:
    return _STATE.strict_tool_result_pairing


def set_strict_tool_result_pairing(value: bool) -> None:
    _STATE.strict_tool_result_pairing = value


def get_session_source() -> str | None:
    return _STATE.session_source


def set_session_source(source: str) -> None:
    _STATE.session_source = source


def get_question_preview_format() -> Literal["markdown", "html"] | None:
    return _STATE.question_preview_format


def set_question_preview_format(fmt: Literal["markdown", "html"]) -> None:
    _STATE.question_preview_format = fmt


def get_agent_color_map() -> dict[str, Any]:
    return _STATE.agent_color_map


def get_flag_settings_path() -> str | None:
    return _STATE.flag_settings_path


def set_flag_settings_path(path: str | None) -> None:
    _STATE.flag_settings_path = path


def get_flag_settings_inline() -> dict[str, Any] | None:
    return _STATE.flag_settings_inline


def set_flag_settings_inline(settings: dict[str, Any] | None) -> None:
    _STATE.flag_settings_inline = settings


def get_session_ingress_token() -> str | None:
    return _STATE.session_ingress_token


def set_session_ingress_token(token: str | None) -> None:
    _STATE.session_ingress_token = token


def get_access_token_from_fd() -> str | None:
    return _STATE.access_token_from_fd


def set_access_token_from_fd(token: str | None) -> None:
    _STATE.access_token_from_fd = token


def get_api_key_from_fd() -> str | None:
    return _STATE.api_key_from_fd


def set_api_key_from_fd(key: str | None) -> None:
    _STATE.api_key_from_fd = key


# --
# Last API request (bug reports / /share)


def set_last_api_request(params: Any | None) -> None:
    _STATE.last_api_request = params


def get_last_api_request() -> Any | None:
    return _STATE.last_api_request


def set_last_api_request_messages(messages: Any | None) -> None:
    _STATE.last_api_request_messages = messages


def get_last_api_request_messages() -> Any | None:
    return _STATE.last_api_request_messages


_MAX_IN_MEMORY_ERRORS = 100


def add_to_in_memory_error_log(error_info: ErrorLogEntry) -> None:
    if len(_STATE.in_memory_error_log) >= _MAX_IN_MEMORY_ERRORS:
        _STATE.in_memory_error_log.pop(0)  # Remove oldest error
    _STATE.in_memory_error_log.append(error_info)


# --
# Allowed setting sources / auth posture


def get_allowed_setting_sources() -> list[SettingSource]:
    return _STATE.allowed_setting_sources


def set_allowed_setting_sources(sources: list[SettingSource]) -> None:
    _STATE.allowed_setting_sources = sources


def prefer_third_party_authentication() -> bool:
    # IDE extension should behave as 1P for authentication reasons.
    return get_is_non_interactive_session() and _STATE.client_type != "tabvis-vscode"


# --
# Session-only flags (bypass / trust / persistence)


def set_session_bypass_permissions_mode(enabled: bool) -> None:
    _STATE.session_bypass_permissions_mode = enabled


def get_session_bypass_permissions_mode() -> bool:
    return _STATE.session_bypass_permissions_mode


def set_session_trust_accepted(accepted: bool) -> None:
    _STATE.session_trust_accepted = accepted


def get_session_trust_accepted() -> bool:
    return _STATE.session_trust_accepted


def set_session_persistence_disabled(disabled: bool) -> None:
    _STATE.session_persistence_disabled = disabled


def is_session_persistence_disabled() -> bool:
    return _STATE.session_persistence_disabled


# --
# Plan mode transitions


def has_exited_plan_mode_in_session() -> bool:
    return _STATE.has_exited_plan_mode


def set_has_exited_plan_mode(value: bool) -> None:
    _STATE.has_exited_plan_mode = value


def needs_plan_mode_exit_attachment() -> bool:
    return _STATE.needs_plan_mode_exit_attachment


def set_needs_plan_mode_exit_attachment(value: bool) -> None:
    _STATE.needs_plan_mode_exit_attachment = value


def handle_plan_mode_transition(from_mode: str, to_mode: str) -> None:
    # If switching TO plan mode, clear any pending exit attachment. This prevents sending both
    # plan_mode and plan_mode_exit when user toggles quickly.
    if to_mode == "plan" and from_mode != "plan":
        _STATE.needs_plan_mode_exit_attachment = False

    # If switching out of plan mode, trigger the plan_mode_exit attachment.
    if from_mode == "plan" and to_mode != "plan":
        _STATE.needs_plan_mode_exit_attachment = True


# --
# SDK init event state


def set_init_json_schema(schema: dict[str, Any]) -> None:
    _STATE.init_json_schema = schema


def get_init_json_schema() -> dict[str, Any] | None:
    return _STATE.init_json_schema


def register_hook_callbacks(hooks: dict[str, list[Any]]) -> None:
    if _STATE.registered_hooks is None:
        _STATE.registered_hooks = {}

    # `register_hook_callbacks` may be called multiple times, so we merge (not overwrite).
    for event, matchers in hooks.items():
        if event not in _STATE.registered_hooks:
            _STATE.registered_hooks[event] = []
        _STATE.registered_hooks[event].extend(matchers)


def get_registered_hooks() -> dict[str, list[Any]] | None:
    return _STATE.registered_hooks


def clear_registered_hooks() -> None:
    _STATE.registered_hooks = None


def reset_sdk_init_state() -> None:
    _STATE.init_json_schema = None
    _STATE.registered_hooks = None


# --
# Plan slug cache / session teams


def get_plan_slug_cache() -> dict[str, str]:
    return _STATE.plan_slug_cache


def get_session_created_teams() -> set[str]:
    return _STATE.session_created_teams


# --
# Teleported session tracking


def set_teleported_session_info(session_id: str | None) -> None:
    _STATE.teleported_session_info = {
        "isTeleported": True,
        "hasLoggedFirstMessage": False,
        "sessionId": session_id,
    }


def get_teleported_session_info() -> TeleportedSessionInfo | None:
    return _STATE.teleported_session_info


def mark_first_teleport_message_logged() -> None:
    if _STATE.teleported_session_info:
        _STATE.teleported_session_info["hasLoggedFirstMessage"] = True


# --
# Invoked skills tracking (preservation across compaction)


def add_invoked_skill(
    skill_name: str,
    skill_path: str,
    content: str,
    agent_id: str | None = None,
) -> None:
    key = f"{agent_id or ''}:{skill_name}"
    _STATE.invoked_skills[key] = {
        "skillName": skill_name,
        "skillPath": skill_path,
        "content": content,
        "invokedAt": _now_ms(),
        "agentId": agent_id,
    }


def get_invoked_skills() -> dict[str, InvokedSkillInfo]:
    return _STATE.invoked_skills


def get_invoked_skills_for_agent(agent_id: str | None) -> dict[str, InvokedSkillInfo]:
    normalized_id = agent_id if agent_id is not None else None
    filtered: dict[str, InvokedSkillInfo] = {}
    for key, skill in _STATE.invoked_skills.items():
        if skill["agentId"] == normalized_id:
            filtered[key] = skill
    return filtered


def clear_invoked_skills(preserved_agent_ids: AbstractSet[str] | None = None) -> None:
    if not preserved_agent_ids or len(preserved_agent_ids) == 0:
        _STATE.invoked_skills.clear()
        return
    for key in list(_STATE.invoked_skills.keys()):
        skill = _STATE.invoked_skills[key]
        if skill["agentId"] is None or skill["agentId"] not in preserved_agent_ids:
            del _STATE.invoked_skills[key]


def clear_invoked_skills_for_agent(agent_id: str) -> None:
    for key in list(_STATE.invoked_skills.keys()):
        if _STATE.invoked_skills[key]["agentId"] == agent_id:
            del _STATE.invoked_skills[key]


# --
# Slow operations tracking (dev bar)

_MAX_SLOW_OPERATIONS = 10
_SLOW_OPERATION_TTL_MS = 10000

# A stable empty reference so callers can bail via identity instead of re-rendering at 2fps.
_EMPTY_SLOW_OPERATIONS: list[SlowOperation] = []


def add_slow_operation(operation: str, duration_ms: int) -> None:
    return


def get_slow_operations() -> list[SlowOperation]:
    # Most common case: nothing tracked. Return a stable reference so the caller's setState()
    # can bail via identity instead of re-rendering at 2fps.
    if len(_STATE.slow_operations) == 0:
        return _EMPTY_SLOW_OPERATIONS
    now = _now_ms()
    # Only allocate a new list when something actually expired; otherwise keep the reference
    # stable across polls while ops are still fresh.
    if any(now - op["timestamp"] >= _SLOW_OPERATION_TTL_MS for op in _STATE.slow_operations):
        _STATE.slow_operations = [
            op for op in _STATE.slow_operations if now - op["timestamp"] < _SLOW_OPERATION_TTL_MS
        ]
        if len(_STATE.slow_operations) == 0:
            return _EMPTY_SLOW_OPERATIONS
    # Safe to return directly: add_slow_operation reassigns slow_operations before appending,
    # so the list held in React state is never mutated.
    return _STATE.slow_operations


# --
# Main thread agent type / remote mode


def get_main_thread_agent_type() -> str | None:
    return _STATE.main_thread_agent_type


def set_main_thread_agent_type(agent_type: str | None) -> None:
    _STATE.main_thread_agent_type = agent_type


def get_is_remote_mode() -> bool:
    return _STATE.is_remote_mode


def set_is_remote_mode(value: bool) -> None:
    _STATE.is_remote_mode = value


# --
# System prompt section cache


def get_system_prompt_section_cache() -> dict[str, str | None]:
    return _STATE.system_prompt_section_cache


def set_system_prompt_section_cache_entry(name: str, value: str | None) -> None:
    _STATE.system_prompt_section_cache[name] = value


def clear_system_prompt_section_state() -> None:
    _STATE.system_prompt_section_cache.clear()


# --
# Last emitted date (midnight date-change detection)


def get_last_emitted_date() -> str | None:
    return _STATE.last_emitted_date


def set_last_emitted_date(date: str | None) -> None:
    _STATE.last_emitted_date = date


# --
# Additional directories for TABVIS.md


def get_additional_directories_for_tabvis_md() -> list[str]:
    return _STATE.additional_directories_for_tabvis_md


def set_additional_directories_for_tabvis_md(directories: list[str]) -> None:
    _STATE.additional_directories_for_tabvis_md = directories


# --
# Prompt-cache 1h allowlist / eligibility / beta header latches


def get_prompt_cache_1h_allowlist() -> list[str] | None:
    return _STATE.prompt_cache_1h_allowlist


def set_prompt_cache_1h_allowlist(allowlist: list[str] | None) -> None:
    _STATE.prompt_cache_1h_allowlist = allowlist


def get_prompt_cache_1h_eligible() -> bool | None:
    return _STATE.prompt_cache_1h_eligible


def set_prompt_cache_1h_eligible(eligible: bool | None) -> None:
    _STATE.prompt_cache_1h_eligible = eligible


def get_cache_editing_header_latched() -> bool | None:
    return _STATE.cache_editing_header_latched


def set_cache_editing_header_latched(v: bool) -> None:
    _STATE.cache_editing_header_latched = v


def get_thinking_clear_latched() -> bool | None:
    return _STATE.thinking_clear_latched


def set_thinking_clear_latched(v: bool) -> None:
    _STATE.thinking_clear_latched = v


def clear_beta_header_latches() -> None:
    """Reset beta header latches to None. Called on /clear and /compact so a fresh conversation
    gets fresh header evaluation."""
    _STATE.cache_editing_header_latched = None
    _STATE.thinking_clear_latched = None


# --
# Prompt id


def get_prompt_id() -> str | None:
    return _STATE.prompt_id


def set_prompt_id(prompt_id: str | None) -> None:
    _STATE.prompt_id = prompt_id
