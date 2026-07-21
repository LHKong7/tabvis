"""Phase 3 — RunOrchestrator: create/start seam and cooperative cancel (design §7.6, §15)."""

from __future__ import annotations

import pytest

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.interaction_service import InteractionService
from tabvis.gateway.runtime.orchestrator import RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore


class _RecordingLauncher:
    """A fake Agent Runtime: records launches/aborts instead of running a model loop."""

    def __init__(self) -> None:
        self.launched: list[str] = []
        self.aborted: list[str] = []

    async def launch(self, run) -> None:
        self.launched.append(run.run_id)

    async def abort(self, run_id: str) -> None:
        self.aborted.append(run_id)


def test_create_and_start_invokes_the_launcher() -> None:
    import asyncio

    async def scenario() -> None:
        launcher = _RecordingLauncher()
        orch = RunOrchestrator(RunStore(), InteractionService(), launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        assert run.status == runs.QUEUED
        assert launcher.launched == [run.run_id]  # the loop runs behind the launcher, not the handler

    asyncio.run(scenario())


def test_create_without_launcher_still_creates_the_run() -> None:
    import asyncio

    async def scenario() -> None:
        orch = RunOrchestrator(RunStore(), InteractionService(), None)  # control-plane-only mode
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        assert orch._runs.get_run(run.run_id).status == runs.QUEUED

    asyncio.run(scenario())


def test_cancel_running_run_goes_through_cancelling_and_aborts_launcher() -> None:
    import asyncio

    async def scenario() -> None:
        launcher = _RecordingLauncher()
        rs = RunStore()
        orch = RunOrchestrator(rs, InteractionService(), launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        rs.transition(run.run_id, runs.PREPARING)
        rs.transition(run.run_id, runs.RUNNING)

        cancelled = await orch.cancel(run.run_id)
        assert cancelled.status == runs.CANCELLED
        assert launcher.aborted == [run.run_id]

    asyncio.run(scenario())


def test_cancel_waiting_run_cancels_the_interaction_too() -> None:
    import asyncio

    async def scenario() -> None:
        rs = RunStore()
        svc = InteractionService(run_store=rs)
        orch = RunOrchestrator(rs, svc, None)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        rs.transition(run.run_id, runs.PREPARING)
        rs.transition(run.run_id, runs.RUNNING)
        interaction = svc.request(run.run_id, "question", {"text": "?"})

        cancelled = await orch.cancel(run.run_id)
        assert cancelled.status == runs.CANCELLED
        assert svc.get(interaction.interaction_id).status == "cancelled"

    asyncio.run(scenario())


def test_cancel_is_idempotent_for_already_cancelled_run() -> None:
    import asyncio

    async def scenario() -> None:
        rs = RunStore()
        orch = RunOrchestrator(rs, InteractionService(), None)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        await orch.cancel(run.run_id)
        again = await orch.cancel(run.run_id)  # no raise, returns the cancelled run
        assert again.status == runs.CANCELLED

    asyncio.run(scenario())


def test_cancel_completed_run_is_a_conflict() -> None:
    import asyncio

    async def scenario() -> None:
        rs = RunStore()
        orch = RunOrchestrator(rs, InteractionService(), None)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1")
        rs.transition(run.run_id, runs.PREPARING)
        rs.transition(run.run_id, runs.RUNNING)
        rs.transition(run.run_id, runs.COMPLETED)
        with pytest.raises(GatewayError) as ei:
            await orch.cancel(run.run_id)
        assert ei.value.code == "RUN_TERMINAL"

    asyncio.run(scenario())
