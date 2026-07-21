"""Phase 0 — typed, prefixed identifiers (design §15 Phase 0)."""

from __future__ import annotations

from tabvis.gateway.protocol import ids


def test_minters_carry_their_prefix() -> None:
    assert ids.new_run_id().startswith("run_")
    assert ids.new_session_id().startswith("ses_")
    assert ids.new_conversation_id().startswith("conv_")
    assert ids.new_interaction_id().startswith("int_")
    assert ids.new_command_id().startswith("cmd_")
    assert ids.new_event_id().startswith("evt_")
    assert ids.new_workspace_id().startswith("ws_")
    assert ids.new_agent_id().startswith("ag_")


def test_ids_are_unique() -> None:
    minted = {ids.new_run_id() for _ in range(1000)}
    assert len(minted) == 1000


def test_ids_have_entropy_beyond_the_prefix() -> None:
    rid = ids.new_run_id()
    assert len(rid) > len("run_")


def test_validators_accept_matching_and_reject_mismatched() -> None:
    assert ids.is_run_id(ids.new_run_id())
    assert not ids.is_run_id(ids.new_session_id())
    assert not ids.is_run_id("run_")  # prefix alone is not a valid id
    assert not ids.is_run_id("")
    assert not ids.is_run_id("nope")
    assert ids.is_session_id(ids.new_session_id())
    assert ids.is_interaction_id(ids.new_interaction_id())
    assert ids.is_command_id(ids.new_command_id())
    assert ids.is_event_id(ids.new_event_id())
