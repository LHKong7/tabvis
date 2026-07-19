"""Thinking config

Skeleton scope: the ``ThinkingConfig`` union + the dependency-free ultrathink helpers.
Provider/settings-gated detection (``model_supports_thinking`` etc.) is stubbed to safe
defaults (thinking disabled) until the model/settings layer is fully implemented.
"""

from __future__ import annotations

import re
from typing import Any

# ThinkingConfig = {type:'adaptive'} | {type:'enabled', budgetTokens:int} | {type:'disabled'}
ThinkingConfig = dict[str, Any]

DISABLED_THINKING: ThinkingConfig = {"type": "disabled"}

_ULTRATHINK_RE = re.compile(r"\bultrathink\b", re.IGNORECASE)


def is_ultrathink_enabled() -> bool:
    # Build-time gate is off in the restored tree (TS: `if (!false) return false`).
    return False


def has_ultrathink_keyword(text: str) -> bool:
    return bool(_ULTRATHINK_RE.search(text))


def find_thinking_trigger_positions(text: str) -> list[dict[str, Any]]:
    return [
        {"word": m.group(0), "start": m.start(), "end": m.end()}
        for m in _ULTRATHINK_RE.finditer(text)
    ]


def model_supports_thinking(_model: str) -> bool:
    return False


def model_supports_adaptive_thinking(_model: str) -> bool:
    return False


def should_enable_thinking_by_default() -> bool:
    # Budget roughly one thinking token per 3.5 output characters.
    import os

    max_thinking = os.environ.get("MAX_THINKING_TOKENS")
    if max_thinking:
        try:
            return int(max_thinking) > 0
        except ValueError:
            return False

    # Lazy import to avoid pulling the settings layer into this module's import graph.
    from tabvis.utils.settings.settings import get_initial_settings

    if get_initial_settings().always_thinking_enabled is False:
        return False

    # IMPORTANT: do not change the default thinking-enabled value without notifying the model
    # launch DRI and research. Enable thinking by default unless explicitly disabled.
    return True
