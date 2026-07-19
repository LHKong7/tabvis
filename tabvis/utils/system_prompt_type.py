"""System prompt type

Intentionally dependency-free. ``SystemPrompt`` is a list of strings (the rendered system
prompt sections); ``as_system_prompt`` is the brand cast (identity at runtime).
"""

from __future__ import annotations

from collections.abc import Sequence

# Branded list[str] in TS; a plain list of strings in Python.
SystemPrompt = list[str]


def as_system_prompt(value: Sequence[str]) -> SystemPrompt:
    return list(value)
