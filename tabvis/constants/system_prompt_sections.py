"""System-prompt section wrappers.

Each section is normally memoized (computed once, cached until ``/clear`` or
``/compact``). For ``--dump-system-prompt`` the prompt is built exactly once, so the
cache is a no-op; this module exposes ``system_prompt_section`` /
``dangerous_uncached_system_prompt_section`` / ``resolve_system_prompt_sections`` and
simply computes each section's value on resolve.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

ComputeFn = Callable[[], "str | None | Awaitable[str | None]"]


@dataclass
class SystemPromptSection:
    name: str
    compute: ComputeFn
    cache_break: bool


def system_prompt_section(name: str, compute: ComputeFn) -> SystemPromptSection:
    """Create a memoized system prompt section."""
    return SystemPromptSection(name=name, compute=compute, cache_break=False)


def dangerous_uncached_system_prompt_section(
    name: str,
    compute: ComputeFn,
    _reason: str,
) -> SystemPromptSection:
    """Create a volatile system prompt section that recomputes every turn."""
    return SystemPromptSection(name=name, compute=compute, cache_break=True)


async def resolve_system_prompt_sections(
    sections: list[SystemPromptSection],
) -> list[str | None]:
    """Resolve all system prompt sections, returning prompt strings (or ``None``)."""

    async def _resolve(section: SystemPromptSection) -> str | None:
        value = section.compute()
        if inspect.isawaitable(value):
            value = await value
        return value

    return list(await asyncio.gather(*(_resolve(s) for s in sections)))
