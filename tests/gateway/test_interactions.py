"""Phase 2 — interactions: pause/resume, idempotency, expiry, cancel (design §5.2, §7.4, §15)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import interactions, runs
from tabvis.gateway.runtime.interaction_service import InteractionService
from tabvis.gateway.runtime.run_store import RunStore


def _running_run(run_store: RunStore, agent_id: str = "ag_1") -> str:
    """Create a run and drive it to ``running`` — the precondition for a pause."""
    run = run_store.create_run(agent_id=agent_id, session_id="ses_1", command_id="cmd_1")
    run_store.transition(run.run_id, runs.PREPARING)
    run_store.transition(run.run_id, runs.RUNNING)
    return run.run_id


def test_request_pauses_the_run_and_persists_the_interaction() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)

    interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "Which env?", "options": ["a", "b"]})
    assert interaction.status == interactions.PENDING
    assert rs.get_run(run_id).status == runs.WAITING_FOR_INPUT
    # durable: a fresh service instance still sees the pending interaction (survives a "refresh").
    assert InteractionService(run_store=rs).get(interaction.interaction_id).is_pending

    types = [e.type for e in get_event_store().read(aggregate_id=interaction.interaction_id)]
    assert types == [EventType.INTERACTION_REQUESTED]


def test_answer_resumes_the_run() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "Which env?"})

    receipt = svc.respond(interaction.interaction_id, {"choice": "a"}, response_command_id="cmd_ans")
    assert receipt.status == interactions.ANSWERED
    assert receipt.duplicate is False
    assert rs.get_run(run_id).status == runs.RUNNING
    assert svc.get(interaction.interaction_id).answer == {"choice": "a"}


def test_duplicate_answer_returns_the_original_receipt() -> None:
    # design §15 Phase 2 acceptance: duplicate answers return the original receipt.
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "?"})

    first = svc.respond(interaction.interaction_id, {"choice": "a"}, response_command_id="cmd_ans")
    replay = svc.respond(interaction.interaction_id, {"choice": "a"}, response_command_id="cmd_ans")
    assert replay.duplicate is True
    assert replay.answered_at == first.answered_at
    # only ONE interaction.answered event was emitted (§16.2: at most one response).
    answered = [e for e in get_event_store().read(aggregate_id=interaction.interaction_id)
                if e.type == EventType.INTERACTION_ANSWERED]
    assert len(answered) == 1


def test_a_different_command_cannot_answer_twice() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "?"})
    svc.respond(interaction.interaction_id, {"choice": "a"}, response_command_id="cmd_ans")
    with pytest.raises(GatewayError) as ei:
        svc.respond(interaction.interaction_id, {"choice": "b"}, response_command_id="cmd_other")
    assert ei.value.code == "INTERACTION_ALREADY_ANSWERED"


def test_ask_question_pauses_survives_refresh_answers_once_and_resumes() -> None:
    # The full §5.2 sequence, driven through an orchestrator-owned future (no HTTP object involved).
    async def scenario() -> None:
        rs = RunStore()
        svc = InteractionService(run_store=rs)
        run_id = _running_run(rs)
        interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "Which env?"})

        async def agent_task() -> dict:
            # the agent blocks on the interaction, not on a connection.
            return await svc.wait(interaction.interaction_id, timeout=2.0)

        waiter = asyncio.create_task(agent_task())
        await asyncio.sleep(0)  # let the waiter start; the run stays paused

        # a "refreshed" client answers via a brand-new service instance.
        InteractionService(run_store=rs).respond(
            interaction.interaction_id, {"choice": "prod"}, response_command_id="cmd_ans"
        )
        answer = await waiter
        assert answer == {"choice": "prod"}
        assert rs.get_run(run_id).status == runs.RUNNING

    asyncio.run(scenario())


def test_approval_deny_fails_the_run() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_APPROVAL, {"action": "download", "url": "https://x"})
    assert rs.get_run(run_id).status == runs.WAITING_FOR_APPROVAL

    svc.respond(interaction.interaction_id, {"allow": False}, response_command_id="cmd_deny")
    run = rs.get_run(run_id)
    assert run.status == runs.FAILED
    assert run.error_code == "approval_denied"


def test_approval_allow_resumes_the_run() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_APPROVAL, {"action": "download"})
    svc.respond(interaction.interaction_id, {"allow": True}, response_command_id="cmd_ok")
    assert rs.get_run(run_id).status == runs.RUNNING


def test_cancel_while_waiting_terminates_run_and_interaction() -> None:
    # design §15 Phase 2 acceptance: cancel while waiting terminates the Run and interaction.
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "?"})

    receipt = svc.cancel_for_run(run_id)
    assert receipt is not None and receipt.status == interactions.CANCELLED
    assert svc.get(interaction.interaction_id).status == interactions.CANCELLED
    assert rs.get_run(run_id).status == runs.CANCELLED
    # answering a cancelled interaction is refused.
    with pytest.raises(GatewayError) as ei:
        svc.respond(interaction.interaction_id, {"choice": "a"}, response_command_id="cmd_late")
    assert ei.value.code == "INTERACTION_CANCELLED"


def test_expiry_cancels_the_run_and_unblocks_waiter() -> None:
    async def scenario() -> None:
        rs = RunStore()
        svc = InteractionService(run_store=rs)
        run_id = _running_run(rs)
        interaction = svc.request(run_id, interactions.KIND_QUESTION, {"text": "?"})

        waiter = asyncio.create_task(svc.wait(interaction.interaction_id, timeout=2.0))
        await asyncio.sleep(0)
        svc.expire(interaction.interaction_id)

        with pytest.raises(GatewayError) as ei:
            await waiter
        assert ei.value.code == "INTERACTION_EXPIRED"
        assert rs.get_run(run_id).status == runs.CANCELLED

    asyncio.run(scenario())


def test_request_requires_a_running_run() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run = rs.create_run(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")  # still queued
    with pytest.raises(GatewayError) as ei:
        svc.request(run.run_id, interactions.KIND_QUESTION, {"text": "?"})
    assert ei.value.code == "CONFLICT"  # CAS expected running, got queued


def test_unknown_kind_is_rejected() -> None:
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    run_id = _running_run(rs)
    with pytest.raises(GatewayError) as ei:
        svc.request(run_id, "chitchat", {"text": "?"})
    assert ei.value.code == "VALIDATION_FAILED"


def test_list_pending_reconstructs_the_waiting_set() -> None:
    # design §5.2 restart recovery reads the pending set from the durable store.
    rs = RunStore()
    svc = InteractionService(run_store=rs)
    a = svc.request(_running_run(rs, "ag_a"), interactions.KIND_QUESTION, {"text": "?"})
    b = svc.request(_running_run(rs, "ag_b"), interactions.KIND_APPROVAL, {"action": "x"})
    svc.request(_running_run(rs, "ag_c"), interactions.KIND_QUESTION, {"text": "?"})

    pending_ids = {i.interaction_id for i in InteractionService(run_store=rs).list_pending()}
    assert {a.interaction_id, b.interaction_id}.issubset(pending_ids)
    assert len(pending_ids) == 3
