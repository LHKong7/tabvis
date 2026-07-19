"""Auto-compaction — token-budget thresholds + the auto-compact trigger.

Cycle note: this module is part of the mutually-recursive compact cycle. Every
cross-cycle reference (``compact.compact_conversation`` /
``CompactionResult`` / ``RecompactionInfo`` / ``ERROR_MESSAGE_USER_ABORT``,
``services.tokens.token_count_with_estimation``,
``services.api.model_client.get_max_output_tokens_for_model``,
``post_compact_cleanup.run_post_compact_cleanup``) is broken with a
``TYPE_CHECKING`` type-only import plus a function-local (lazy) runtime import,
so this module imports standalone even before its siblings exist on disk.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict

from tabvis.bootstrap.state import get_sdk_betas
from tabvis.utils.context import get_context_window_for_model
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy

if TYPE_CHECKING:  # type-only — never imported at runtime
    from tabvis.constants.query_source import QuerySource
    from tabvis.agent.compact.compact import CompactionResult
    from tabvis.tool import ToolUseContext
    from tabvis.types.message import Message


# Reserve this many tokens for output during compaction. Based on p99.99 of
# compact summary output being 17,387 tokens.
MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000


def _get_max_output_tokens_for_model(model: str) -> int:
    """Lazy proxy for the cycle-sibling ``get_max_output_tokens_for_model``.

    ``model_client`` may not expose the per-model variant yet; fall back to
    the model-max-output upper-limit computation in ``utils/context``.
    """
    try:
        from tabvis.agent.api.model_client import (  # type: ignore[attr-defined]
            get_max_output_tokens_for_model,
        )

        return get_max_output_tokens_for_model(model)
    except (ImportError, AttributeError):
        from tabvis.utils.context import get_model_max_output_tokens

        return get_model_max_output_tokens(model)["default"]


def get_effective_context_window_size(model: str) -> int:
    """Context window size minus the max output tokens reserved for the summary."""
    reserved_tokens_for_summary = min(
        _get_max_output_tokens_for_model(model),
        MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    )
    context_window = get_context_window_for_model(model, get_sdk_betas())

    auto_compact_window = os.environ.get("TABVIS_AUTO_COMPACT_WINDOW")
    if auto_compact_window:
        try:
            parsed = int(auto_compact_window, 10)
        except ValueError:
            parsed = 0
        if parsed > 0:
            context_window = min(context_window, parsed)

    return context_window - reserved_tokens_for_summary


class AutoCompactTrackingState(TypedDict, total=False):
    compacted: bool
    turnCounter: int
    # Unique ID per turn.
    turnId: str
    # Consecutive autocompact failures. Reset on success. Circuit breaker.
    consecutiveFailures: int


AUTOCOMPACT_BUFFER_TOKENS = 13_000
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000
MANUAL_COMPACT_BUFFER_TOKENS = 3_000

# Stop trying autocompact after this many consecutive failures.
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


def get_auto_compact_threshold(model: str) -> int:
    effective_context_window = get_effective_context_window_size(model)

    autocompact_threshold = effective_context_window - AUTOCOMPACT_BUFFER_TOKENS

    # Override for easier testing of autocompact.
    env_percent = os.environ.get("TABVIS_AUTOCOMPACT_PCT_OVERRIDE")
    if env_percent:
        try:
            parsed = float(env_percent)
        except ValueError:
            parsed = float("nan")
        if not math.isnan(parsed) and 0 < parsed <= 100:
            percentage_threshold = math.floor(effective_context_window * (parsed / 100))
            return min(percentage_threshold, autocompact_threshold)

    return autocompact_threshold


@dataclass
class TokenWarningState:
    """Token-budget warning flags."""

    percent_left: int
    is_above_warning_threshold: bool
    is_above_error_threshold: bool
    is_above_auto_compact_threshold: bool
    is_at_blocking_limit: bool


def calculate_token_warning_state(token_usage: int, model: str) -> TokenWarningState:
    auto_compact_threshold = get_auto_compact_threshold(model)
    threshold = (
        auto_compact_threshold
        if is_auto_compact_enabled()
        else get_effective_context_window_size(model)
    )

    percent_left = max(0, round(((threshold - token_usage) / threshold) * 100))

    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS

    is_above_warning_threshold = token_usage >= warning_threshold
    is_above_error_threshold = token_usage >= error_threshold

    is_above_auto_compact_threshold = (
        is_auto_compact_enabled() and token_usage >= auto_compact_threshold
    )

    actual_context_window = get_effective_context_window_size(model)
    default_blocking_limit = actual_context_window - MANUAL_COMPACT_BUFFER_TOKENS

    # Allow override for testing.
    blocking_limit_override = os.environ.get("TABVIS_BLOCKING_LIMIT_OVERRIDE")
    parsed_override = math.nan
    if blocking_limit_override:
        try:
            parsed_override = int(blocking_limit_override, 10)
        except ValueError:
            parsed_override = math.nan
    blocking_limit = (
        parsed_override
        if (not math.isnan(parsed_override) and parsed_override > 0)
        else default_blocking_limit
    )

    is_at_blocking_limit = token_usage >= blocking_limit

    return TokenWarningState(
        percent_left=percent_left,
        is_above_warning_threshold=is_above_warning_threshold,
        is_above_error_threshold=is_above_error_threshold,
        is_above_auto_compact_threshold=is_above_auto_compact_threshold,
        is_at_blocking_limit=is_at_blocking_limit,
    )


def is_auto_compact_enabled() -> bool:
    if is_env_truthy(os.environ.get("DISABLE_COMPACT")):
        return False
    # Allow disabling just auto-compact (keeps manual /compact working).
    if is_env_truthy(os.environ.get("DISABLE_AUTO_COMPACT")):
        return False
    # Check if user has disabled auto-compact in their settings.
    return _get_auto_compact_enabled_config()


def _get_auto_compact_enabled_config() -> bool:
    """Read ``autoCompactEnabled`` from the global config (default ``True``)."""
    try:
        from tabvis.utils.settings.settings import get_global_config

        config = get_global_config()
    except Exception:  # noqa: BLE001 — config read must never break the loop
        return True
    value = config.get("autoCompactEnabled", True)
    return bool(value)


async def should_auto_compact(
    messages: list[Message],
    model: str,
    query_source: QuerySource | None = None,
    # Snip removes messages but the surviving assistant's usage still reflects
    # pre-snip context. Subtract the rough-delta that snip already computed.
    snip_tokens_freed: int = 0,
) -> bool:
    # Recursion guards. session_memory and compact are forked agents that would
    # deadlock.
    if query_source in ("session_memory", "compact"):
        return False

    if not is_auto_compact_enabled():
        return False

    token_count = _token_count_with_estimation(messages) - snip_tokens_freed
    threshold = get_auto_compact_threshold(model)
    effective_window = get_effective_context_window_size(model)

    snip_suffix = f" snipFreed={snip_tokens_freed}" if snip_tokens_freed > 0 else ""
    log_for_debugging(
        f"autocompact: tokens={token_count} threshold={threshold} "
        f"effectiveWindow={effective_window}{snip_suffix}"
    )

    warning_state = calculate_token_warning_state(token_count, model)
    return warning_state.is_above_auto_compact_threshold


def _token_count_with_estimation(messages: list[Message]) -> int:
    """Lazy proxy for the cycle-sibling token estimator."""
    from tabvis.utils.tokens import token_count_with_estimation

    return token_count_with_estimation(messages)


class AutoCompactResult(TypedDict, total=False):
    wasCompacted: bool
    compactionResult: CompactionResult
    consecutiveFailures: int


async def auto_compact_if_needed(
    messages: list[Message],
    tool_use_context: ToolUseContext,
    cache_safe_params: Any,
    query_source: QuerySource | None = None,
    tracking: AutoCompactTrackingState | None = None,
    snip_tokens_freed: int | None = None,
) -> AutoCompactResult:
    if is_env_truthy(os.environ.get("DISABLE_COMPACT")):
        return {"wasCompacted": False}

    # Circuit breaker: stop retrying after N consecutive failures.
    if (
        tracking is not None
        and tracking.get("consecutiveFailures") is not None
        and tracking["consecutiveFailures"] >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    ):
        return {"wasCompacted": False}

    model = tool_use_context.options.main_loop_model
    should_compact = await should_auto_compact(
        messages,
        model,
        query_source,
        snip_tokens_freed or 0,
    )

    if not should_compact:
        return {"wasCompacted": False}

    # Lazy: compact is the cycle hub.
    from tabvis.agent.compact.compact import compact_conversation

    recompaction_info: dict[str, Any] = {
        "isRecompactionInChain": (tracking or {}).get("compacted") is True,
        "turnsSincePreviousCompact": (tracking or {}).get("turnCounter", -1),
        "previousCompactTurnId": (tracking or {}).get("turnId"),
        "autoCompactThreshold": get_auto_compact_threshold(model),
        "querySource": query_source,
    }

    try:
        compaction_result = await compact_conversation(
            messages,
            tool_use_context,
            cache_safe_params,
            True,  # Suppress user questions for autocompact.
            None,  # No custom instructions for autocompact.
            True,  # is_auto_compact
            recompaction_info,
        )

        from tabvis.agent.compact.post_compact_cleanup import run_post_compact_cleanup

        run_post_compact_cleanup(query_source)

        return {
            "wasCompacted": True,
            "compactionResult": compaction_result,
            # Reset failure count on success.
            "consecutiveFailures": 0,
        }
    except Exception as error:  # noqa: BLE001 — catch-all so a failed attempt never crashes the caller
        from tabvis.agent.compact.compact import ERROR_MESSAGE_USER_ABORT

        if not _has_exact_error_message(error, ERROR_MESSAGE_USER_ABORT):
            from tabvis.utils.log import log_error

            log_error(error)
        # Increment consecutive failure count for circuit breaker.
        prev_failures = (tracking or {}).get("consecutiveFailures", 0)
        next_failures = prev_failures + 1
        if next_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            log_for_debugging(
                f"autocompact: circuit breaker tripped after {next_failures} "
                "consecutive failures — skipping future attempts this session"
            )
        return {"wasCompacted": False, "consecutiveFailures": next_failures}


def _has_exact_error_message(error: Any, message: str) -> bool:
    """True when ``error`` is an exception whose message exactly equals ``message``."""
    return isinstance(error, Exception) and str(error) == message
