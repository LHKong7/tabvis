"""Auto-compaction must actually run end to end, not fail open on every attempt.

``compact_conversation`` reaches its collaborators through lazy cycle-breaking proxies in
``compact.py``. Those proxies are only exercised at runtime, and ``auto_compact_if_needed``
swallows every exception, so a missing symbol turns compaction into a silent no-op that no
existing test notices — the run just grows until the model rejects the prompt as too long.

These tests pin the whole path: the proxies resolve, one compaction produces a usable message
list, and that list survives the API projection that runs on the very next turn.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tabvis.agent.api.model_client import _messages_to_api_params
from tabvis.agent.compact import compact as compact_module
from tabvis.agent.compact.auto_compact import auto_compact_if_needed
from tabvis.agent.compact.compact import (
    build_post_compact_messages,
    compact_conversation,
)
from tabvis.tool import ToolUseContext, ToolUseContextOptions
from tabvis.utils.messages import (
    create_assistant_message,
    create_user_message,
    get_last_assistant_message,
    get_messages_after_compact_boundary,
    is_compact_boundary_message,
    normalize_messages_for_api,
)

SUMMARY = "The agent read three files and opened two pages."


def _context() -> ToolUseContext:
    return ToolUseContext(
        options=ToolUseContextOptions(tools=[], main_loop_model="test-model"),
        messages=[],
    )


def _conversation() -> list[dict[str, Any]]:
    return [
        create_user_message(content="Research the audit benchmark."),
        create_assistant_message(content="Reading the first source."),
        create_user_message(content="Keep going."),
        create_assistant_message(content="Reading the second source."),
    ]


@pytest.fixture
def stub_summary_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the model stream; every other collaborator stays real."""

    async def fake_stream(**_kwargs: Any):
        yield create_assistant_message(content=SUMMARY)

    monkeypatch.setattr(compact_module, "_query_model_with_streaming", fake_stream)


def test_every_lazy_proxy_resolves() -> None:
    """Each proxy imports a symbol that must exist; a missing one raises ImportError at runtime."""
    assert asyncio.run(compact_module._execute_pre_compact_hooks({"trigger": "auto"}, None))
    assert asyncio.run(compact_module._execute_post_compact_hooks({"trigger": "auto"}, None))
    assert compact_module._create_compact_boundary_message("auto", 1234)["subtype"] == (
        "compact_boundary"
    )
    assert compact_module._get_last_assistant_message([]) is None
    assert compact_module._get_messages_after_compact_boundary([]) == []
    assert compact_module._is_compact_boundary_message({}) is False


def test_compaction_completes_and_yields_a_usable_conversation(
    stub_summary_model: None,
) -> None:
    result = asyncio.run(
        compact_conversation(_conversation(), _context(), None, True, None, True, None)
    )

    assert is_compact_boundary_message(result["boundaryMarker"])
    assert result["boundaryMarker"]["compactMetadata"]["trigger"] == "auto"
    assert SUMMARY in str(result["summaryMessages"])

    compacted = build_post_compact_messages(result)
    assert compacted[0] is result["boundaryMarker"]

    # The next turn projects this straight to the API. Attachment/hook envelopes carry no
    # ``message`` payload, so an unfiltered pass-through raises KeyError here.
    params = _messages_to_api_params(compacted, [], False, "test")
    assert params, "the compacted conversation must survive the API projection"
    assert all(p["role"] in ("user", "assistant") for p in params)


def test_auto_compact_reports_success_rather_than_failing_open(
    stub_summary_model: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def always_compact(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(
        "tabvis.agent.compact.auto_compact.should_auto_compact", always_compact
    )
    result = asyncio.run(auto_compact_if_needed(_conversation(), _context(), None))
    assert result["wasCompacted"] is True, result


def test_boundary_helpers_split_on_the_last_boundary() -> None:
    boundary = compact_module._create_compact_boundary_message("manual", 10)
    before = create_user_message(content="old")
    after = create_assistant_message(content="new")

    assert get_messages_after_compact_boundary([before, boundary, after]) == [after]
    assert get_messages_after_compact_boundary([before]) == [before]
    assert get_last_assistant_message([before, after]) is after
    # The boundary is a system envelope, so it never reaches the model.
    assert normalize_messages_for_api([boundary, after]) == [after]
