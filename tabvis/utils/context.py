"""Context-window + max-output-token resolution

Faithful capability table keyed by model string: resolves the effective context window
(default 200k, 1M opt-ins via ``[1m]`` suffix / beta header / sonnet-4-6 experiment / ant
override / on-disk capability cache) and the per-model default/upper max-output-token pair.

Faithful-behavior notes:
- ``resolveAntModel`` (from ``src/utils/model/antModels.ts``) is NOT imported by the TS
  source shown and is ant-only dead code in the headless non-ant tree (every call site is
  guarded by ``USER_TYPE === 'ant'``). ``antModels.ts`` is also not implemented. A faithful local
  fallback :func:`_resolve_ant_model` returns ``None`` whenever ``USER_TYPE != 'ant'`` (i.e.
  always, here).
- ``Math.round`` is banker's-rounding-free half-up; Python ``round`` is banker's — the
  percentage helper reimplements half-up to match JS exactly.

Casing: Python identifiers snake_case; the constants keep the TS UPPER_CASE spelling (they
are module constants, naming-lint-relevant in utils, so adapted to UPPER_CASE which is the
TS spelling already).
"""

from __future__ import annotations

import math
import os
from typing import Any

from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.model.model import get_canonical_name
from tabvis.utils.model.model_capabilities import get_model_capability

# The 1M-context beta header (inlined from the removed constants/betas.py).
CONTEXT_1M_BETA_HEADER = "context-1m-2025-08-07"

# Model context window size (200k tokens for all models right now).
MODEL_CONTEXT_WINDOW_DEFAULT = 200_000

# Maximum output tokens for compact operations.
COMPACT_MAX_OUTPUT_TOKENS = 20_000

# Default max output tokens.
_MAX_OUTPUT_TOKENS_DEFAULT = 32_000
_MAX_OUTPUT_TOKENS_UPPER_LIMIT = 64_000

# Capped default for slot-reservation optimization. BQ p99 output = 4,911 tokens, so 32k/64k
# defaults over-reserve 8-16x slot capacity. With the cap enabled, <1% of requests hit the
# limit; those get one clean retry at 64k (see query.ts max_output_tokens_escalate). Cap is
# applied in modelClient.ts:getMaxOutputTokensForModel to avoid the
# growthbook->betas->context import cycle.
CAPPED_DEFAULT_MAX_TOKENS = 8_000
ESCALATED_MAX_TOKENS = 64_000


def _resolve_ant_model(model: str | None) -> Any | None:
    """Faithful fallback for ``resolveAntModel`` (ant-only; ``None`` in the non-ant tree).

    The TS resolver returns ``undefined`` whenever ``USER_TYPE !== 'ant'`` and otherwise
    searches a GrowthBook-provided ant model registry (empty here). ``antModels.ts`` is not
    implemented; this stub preserves the only behavior reachable in the headless non-ant tree.
    """
    return None


def _js_round(value: float) -> int:
    """JS ``Math.round`` — half-up toward +infinity (differs from Python banker's round)."""
    return math.floor(value + 0.5)


def is_1m_context_disabled() -> bool:
    """Check if 1M context is disabled via environment variable.

    Used by C4E admins to disable 1M context for HIPAA compliance.
    """
    return is_env_truthy(os.environ.get("TABVIS_DISABLE_1M_CONTEXT"))


def has_1m_context(model: str) -> bool:
    if is_1m_context_disabled():
        return False
    return "[1m]" in model.lower()


# @[MODEL LAUNCH]: Update this pattern if the new model supports 1M context.
def model_supports_1m(model: str) -> bool:
    if is_1m_context_disabled():
        return False
    canonical = get_canonical_name(model)
    return "claude-sonnet-4" in canonical or "opus-4-6" in canonical


def get_context_window_for_model(model: str, betas: list[str] | None = None) -> int:

    # [1m] suffix — explicit client-side opt-in, respected over all detection.
    if has_1m_context(model):
        return 1_000_000

    cap = get_model_capability(model)
    if cap is not None and cap.max_input_tokens and cap.max_input_tokens >= 100_000:
        if cap.max_input_tokens > MODEL_CONTEXT_WINDOW_DEFAULT and is_1m_context_disabled():
            return MODEL_CONTEXT_WINDOW_DEFAULT
        return cap.max_input_tokens

    if betas is not None and CONTEXT_1M_BETA_HEADER in betas and model_supports_1m(model):
        return 1_000_000
    if get_sonnet_1m_exp_treatment_enabled(model):
        return 1_000_000
    return MODEL_CONTEXT_WINDOW_DEFAULT


def get_sonnet_1m_exp_treatment_enabled(model: str) -> bool:
    if is_1m_context_disabled():
        return False
    # Only applies to sonnet 4.6 without an explicit [1m] suffix.
    if has_1m_context(model):
        return False
    if "sonnet-4-6" not in get_canonical_name(model):
        return False
    # TS imports getGlobalConfig from ./config.js; the Python implementation hosts it in the settings
    # module (tabvis.utils.config has no get_global_config). Lazy import keeps it cycle-safe.
    from tabvis.utils.settings.settings import get_global_config  # noqa: PLC0415

    client_data_cache = get_global_config().get("clientDataCache") or {}
    return client_data_cache.get("coral_reef_sonnet") == "true"


def calculate_context_percentages(
    current_usage: dict[str, int] | None,
    context_window_size: int,
) -> dict[str, int | None]:
    """Calculate context window usage percentage from token usage data.

    Returns used and remaining percentages, or ``None`` values if no usage data. The input
    dict (when present) carries the Anthropic snake_case keys ``input_tokens`` /
    ``cache_creation_input_tokens`` / ``cache_read_input_tokens``.
    """
    if not current_usage:
        return {"used": None, "remaining": None}

    total_input_tokens = (
        current_usage["input_tokens"]
        + current_usage["cache_creation_input_tokens"]
        + current_usage["cache_read_input_tokens"]
    )

    used_percentage = _js_round((total_input_tokens / context_window_size) * 100)
    clamped_used = min(100, max(0, used_percentage))

    return {
        "used": clamped_used,
        "remaining": 100 - clamped_used,
    }


def get_model_max_output_tokens(model: str) -> dict[str, int]:
    """Returns the model's default and upper limit for max output tokens.

    The returned dict uses the wire keys ``default`` / ``upperLimit`` (camelCase) so the shape
    round-trips with the TS ``{ default, upperLimit }`` object verbatim.
    """
    default_tokens: int
    upper_limit: int

    m = get_canonical_name(model)

    if "opus-4-6" in m:
        default_tokens = 64_000
        upper_limit = 128_000
    elif "sonnet-4-6" in m:
        default_tokens = 32_000
        upper_limit = 128_000
    elif "opus-4-5" in m or "sonnet-4" in m or "haiku-4" in m:
        default_tokens = 32_000
        upper_limit = 64_000
    elif "opus-4-1" in m or "opus-4" in m:
        default_tokens = 32_000
        upper_limit = 32_000
    elif "claude-3-opus" in m:
        default_tokens = 4_096
        upper_limit = 4_096
    elif "claude-3-sonnet" in m:
        default_tokens = 8_192
        upper_limit = 8_192
    elif "claude-3-haiku" in m:
        default_tokens = 4_096
        upper_limit = 4_096
    elif "3-5-sonnet" in m or "3-5-haiku" in m:
        default_tokens = 8_192
        upper_limit = 8_192
    elif "3-7-sonnet" in m:
        default_tokens = 32_000
        upper_limit = 64_000
    else:
        default_tokens = _MAX_OUTPUT_TOKENS_DEFAULT
        upper_limit = _MAX_OUTPUT_TOKENS_UPPER_LIMIT

    cap = get_model_capability(model)
    if cap is not None and cap.max_tokens and cap.max_tokens >= 4_096:
        upper_limit = cap.max_tokens
        default_tokens = min(default_tokens, upper_limit)

    return {"default": default_tokens, "upperLimit": upper_limit}


def get_max_thinking_tokens_for_model(model: str) -> int:
    """Returns the max thinking budget tokens for a given model.

    The max thinking tokens should be strictly less than the max output tokens. Deprecated
    since newer models use adaptive thinking rather than a strict thinking token budget.
    """
    return get_model_max_output_tokens(model)["upperLimit"] - 1
