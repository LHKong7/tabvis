"""Time-based microcompact config.

GrowthBook config for time-based microcompact. Triggers content-clearing microcompact when the gap
since the last main-loop assistant message exceeds a threshold — the server-side prompt cache has
almost certainly expired, so the full prefix will be rewritten anyway. Clearing old tool results
before the request shrinks what gets rewritten.

Runs before the model API call, upstream of the request dispatch, so the shrunk prompt is what
actually gets sent. Main thread only — subagents have short lifetimes where gap-based eviction
doesn't apply.

The flag (``tengu_slate_heron``) is OFF in the shipped tree; the GrowthBook reader returns the
supplied default, so this resolves to the disabled defaults.
"""

from __future__ import annotations

from typing import TypedDict


class TimeBasedMCConfig(TypedDict):
    """Master switch + gap threshold + retention for time-based microcompact."""

    # Master switch. When False, time-based microcompact is a no-op.
    enabled: bool
    # Trigger when (now - last assistant timestamp) exceeds this many minutes. 60 is the safe
    # choice: the server's 1h cache TTL is guaranteed expired for all users.
    gapThresholdMinutes: int
    # Keep this many most-recent compactable tool results.
    keepRecent: int


TIME_BASED_MC_CONFIG_DEFAULTS: TimeBasedMCConfig = {
    "enabled": False,
    "gapThresholdMinutes": 60,
    "keepRecent": 5,
}


def get_time_based_mc_config() -> TimeBasedMCConfig:
    # Hoist the GB read so exposure fires on every eval path, not just when the caller's other
    # conditions (querySource, messages.length) pass.
    return TIME_BASED_MC_CONFIG_DEFAULTS
