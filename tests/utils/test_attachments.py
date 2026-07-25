"""Focused tests for attachment history classification."""

from __future__ import annotations

from tabvis.utils.attachments import get_plan_mode_attachment_turn_count


def _user(content, *, is_meta: bool = False):
    return {
        "type": "user",
        "isMeta": is_meta,
        "message": {"content": content},
    }


def test_plan_mode_turn_count_ignores_tool_results_and_meta_messages() -> None:
    messages = [
        {"type": "attachment", "attachment": {"type": "plan_mode"}},
        _user("first prompt"),
        _user([{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]),
        _user("internal metadata", is_meta=True),
        _user([{"type": "text", "text": "second prompt"}]),
    ]

    assert get_plan_mode_attachment_turn_count(messages) == {
        "turnCount": 2,
        "foundPlanModeAttachment": True,
    }


def test_plan_mode_turn_count_handles_history_without_attachment() -> None:
    messages = [
        _user([{"type": "tool_result", "tool_use_id": "tool-1", "content": "ok"}]),
        _user("only real prompt"),
    ]

    assert get_plan_mode_attachment_turn_count(messages) == {
        "turnCount": 1,
        "foundPlanModeAttachment": False,
    }
