"""Skill-usage ranking

Records how often / how recently each skill (prompt command) is used, and scores it with a
7-day-half-life exponential decay so the typeahead can float recently-used skills to the top.

Persisted under ``skillUsage`` in the global config (``~/.tabvis.json``):
``{ <skillName>: { usageCount: int, lastUsedAt: epoch_ms } }`` — kept with its TS wire keys
verbatim (round-trips to the on-disk JSON the TS writes / reads).

Following the established ``utils/user._get_or_create_user_id`` precedent, this module reads/writes
the global ``.tabvis.json`` directly. The process-lifetime debounce cache (``last_write_by_skill``) is
reproduced exactly.
"""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

__all__ = [
    "get_skill_usage_score",
    "record_skill_usage",
]

SKILL_USAGE_DEBOUNCE_MS = 60_000

# Process-lifetime debounce cache — avoids lock + read + parse on debounced calls.
# Same pattern as lastConfigStatTime / globalConfigWriteCount in config.ts.
_last_write_by_skill: dict[str, float] = {}


def _now_ms() -> int:
    """``Date.now()`` parity — integer epoch milliseconds."""
    return int(time.time() * 1000)


def _global_tabvis_file() -> str:
    """Path to the global ``.tabvis.json`` (``TABVIS_CONFIG_DIR`` or home), matching ``env.get_global_tabvis_file``."""
    base = os.environ.get("TABVIS_CONFIG_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".tabvis.json")


def _read_global_config() -> dict[str, Any]:
    path = _global_tabvis_file()
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
            if isinstance(loaded, dict):
                return loaded
    except (OSError, ValueError):
        pass
    return {}


def record_skill_usage(skill_name: str) -> None:
    """Record a skill usage for ranking. Updates both usage count and last-used timestamp."""
    now = _now_ms()
    last_write = _last_write_by_skill.get(skill_name)
    # The ranking algorithm uses a 7-day half-life, so sub-minute granularity is
    # irrelevant. Bail out before the write to avoid lock + file I/O.
    if last_write is not None and now - last_write < SKILL_USAGE_DEBOUNCE_MS:
        return
    _last_write_by_skill[skill_name] = now

    config = _read_global_config()
    skill_usage = config.get("skillUsage")
    if not isinstance(skill_usage, dict):
        skill_usage = {}
    existing = skill_usage.get(skill_name)
    existing_count = (
        existing.get("usageCount", 0) if isinstance(existing, dict) else 0
    )
    skill_usage[skill_name] = {
        "usageCount": existing_count + 1,
        "lastUsedAt": now,
    }
    config["skillUsage"] = skill_usage

    path = _global_tabvis_file()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
    except OSError:
        # Persistence is best-effort; the in-memory debounce still holds for this process.
        pass


def get_skill_usage_score(skill_name: str) -> float:
    """Compute a usage score from frequency + recency (7-day-half-life exponential decay).

    Higher scores indicate more frequently and recently used skills.
    """
    config = _read_global_config()
    skill_usage = config.get("skillUsage")
    usage = skill_usage.get(skill_name) if isinstance(skill_usage, dict) else None
    if not usage or not isinstance(usage, dict):
        return 0

    # Recency decay: halve score every 7 days.
    days_since_use = (_now_ms() - usage["lastUsedAt"]) / (1000 * 60 * 60 * 24)
    recency_factor = math.pow(0.5, days_since_use / 7)

    # Minimum recency factor of 0.1 to avoid completely dropping old but heavily used skills.
    return usage["usageCount"] * max(recency_factor, 0.1)
