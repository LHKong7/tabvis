"""Base-prompt migration: get_system_prompt can suppress project instructions + memory (design §11).

When the gateway's Context Runtime owns project context, `stream_agent` calls `get_system_prompt` with
`include_project_instructions=False, include_memory=False` so those sections are not double-emitted.
This verifies the toggles at the source.
"""

from __future__ import annotations

import asyncio

import pytest

from tabvis.constants import prompts


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


def _prompt(**kwargs) -> str:
    sections = asyncio.run(prompts.get_system_prompt([_Tool("Bash")], "m", None, None, **kwargs))
    return "\n".join(s for s in sections if s)


@pytest.fixture(autouse=True)
def _stub_loaders(monkeypatch: pytest.MonkeyPatch):
    async def pi(additional_directories=None):
        return "PROJECT_INSTRUCTIONS_SENTINEL"

    async def mem():
        return "MEMORY_SENTINEL"

    monkeypatch.setattr(prompts, "load_project_instructions_prompt", pi)
    monkeypatch.setattr(prompts, "load_memory_prompt", mem)


def test_included_by_default() -> None:
    text = _prompt()
    assert "PROJECT_INSTRUCTIONS_SENTINEL" in text
    assert "MEMORY_SENTINEL" in text


def test_suppressed_when_caller_owns_project_context() -> None:
    text = _prompt(include_project_instructions=False, include_memory=False)
    assert "PROJECT_INSTRUCTIONS_SENTINEL" not in text
    assert "MEMORY_SENTINEL" not in text


def test_toggles_are_independent() -> None:
    only_mem = _prompt(include_project_instructions=False)
    assert "PROJECT_INSTRUCTIONS_SENTINEL" not in only_mem
    assert "MEMORY_SENTINEL" in only_mem
