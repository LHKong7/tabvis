"""Resume Plus items 1–2 — Gateway session retention + resume_mode threading + skip_browser_init.

Verifies the ``run.create`` handler continues the prior transcript lineage on a resume (instead of
minting a fresh session that ``resume=True`` could never find), that ``resume_mode`` flows through the
orchestrator into the LaunchContext, and that acquiring a browser binding tells the inner loop to skip
its own browser init/teardown.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tabvis.gateway.auth.principals import local_admin
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.methods.runs import RunCreateHandler
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agent.runner import AgentRunLauncher
from tabvis.gateway.runtime.orchestrator import RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore


class _CapturingOrch:
    """Stands in for the orchestrator: records the kwargs the handler resolved."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create_and_start(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return type("R", (), {"to_dict": lambda self: {"run_id": "run_x"}})()


def _cmd(data: dict[str, Any]) -> Command:
    return Command(type=CommandType.RUN_CREATE, data=data, command_id="cmd_1")


def _ctx() -> CommandContext:
    return CommandContext(principal=local_admin())


# --------------------------------------------------------------------------- session retention


def test_resume_from_session_id_retains_lineage() -> None:
    orch = _CapturingOrch()
    h = RunCreateHandler(orchestrator=orch)  # type: ignore[arg-type]
    asyncio.run(h.handle(_cmd({"agent_id": "ag_1", "prompt": "go",
                               "resume_from_session_id": "ses_prev"}), _ctx()))
    call = orch.calls[0]
    assert call["session_id"] == "ses_prev"
    assert call["resume"] is True and call["resume_mode"] == "plus"


def test_explicit_resume_mode_conversation_only() -> None:
    orch = _CapturingOrch()
    h = RunCreateHandler(orchestrator=orch)  # type: ignore[arg-type]
    asyncio.run(h.handle(_cmd({"agent_id": "ag_1", "prompt": "go", "session_id": "ses_a",
                               "resume_mode": "conversation_only"}), _ctx()))
    call = orch.calls[0]
    assert call["session_id"] == "ses_a"
    assert call["resume"] is True and call["resume_mode"] == "conversation_only"


def test_fresh_run_mints_new_session() -> None:
    orch = _CapturingOrch()
    h = RunCreateHandler(orchestrator=orch)  # type: ignore[arg-type]
    asyncio.run(h.handle(_cmd({"agent_id": "ag_1", "prompt": "go"}), _ctx()))
    call = orch.calls[0]
    assert call["session_id"] and call["session_id"].startswith("ses_")
    assert call["resume"] is False and call["resume_mode"] == "fresh"


def test_resume_without_session_is_rejected() -> None:
    orch = _CapturingOrch()
    h = RunCreateHandler(orchestrator=orch)  # type: ignore[arg-type]
    with pytest.raises(GatewayError) as ei:
        asyncio.run(h.handle(_cmd({"agent_id": "ag_1", "prompt": "go", "resume": True}), _ctx()))
    assert ei.value.code == "VALIDATION_FAILED"


# --------------------------------------------------------------------------- resume_mode threading


class _FakeDriver:
    async def launch(self, spec: Any) -> None: ...
    async def execute(self, profile_key: str, intent: Any) -> dict: return {}
    async def verify_identity(self, profile_key: str) -> bool: return True
    async def close(self, profile_key: str) -> None: ...


def test_acquired_binding_sets_skip_browser_init() -> None:
    from tabvis.gateway.runtime.browser.runtime import BrowserRuntime

    async def scenario() -> None:
        rs = RunStore()
        captured: dict[str, Any] = {}

        async def capture(run: Any, context: Any) -> Any:
            # the BrowserRuntime owns init/release, so the inner loop must be told to skip its own.
            captured["skip"] = context.extra.get("skip_browser_init")
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(
            run_store=rs, stream_fn=capture, browser_runtime=BrowserRuntime(driver=_FakeDriver()),
        )
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go"
        )
        await launcher.join(run.run_id)
        assert captured["skip"] is True

    asyncio.run(scenario())


def test_no_binding_leaves_browser_init_to_the_loop() -> None:
    async def scenario() -> None:
        rs = RunStore()
        captured: dict[str, Any] = {}

        async def capture(run: Any, context: Any) -> Any:
            captured["skip"] = context.extra.get("skip_browser_init")
            yield {"type": "result", "result": "done"}

        # No browser_runtime wired → the loop owns browser init (default composition).
        launcher = AgentRunLauncher(run_store=rs, stream_fn=capture)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go"
        )
        await launcher.join(run.run_id)
        assert not captured["skip"]

    asyncio.run(scenario())


def test_resume_mode_reaches_launch_context() -> None:
    async def scenario() -> None:
        rs = RunStore()
        captured: dict[str, Any] = {}

        async def capture(run: Any, context: Any) -> Any:
            captured["resume_mode"] = context.resume_mode
            captured["resume"] = context.resume
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(run_store=rs, stream_fn=capture)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go",
            resume=True, resume_mode="plus",
        )
        await launcher.join(run.run_id)
        assert rs.get_run(run.run_id).status == runs.COMPLETED
        assert captured["resume_mode"] == "plus" and captured["resume"] is True

    asyncio.run(scenario())
