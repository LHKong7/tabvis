"""Feeding the Context Runtime into the model call path via the launcher (design §11).

With a SourceCollector wired, the launcher assembles a Context Pack before the model call, injects its
situational sections into the run's system context, and emits a durable context.pack.built event — all
verified here with fake sources and a capturing stream (no real model).
"""

from __future__ import annotations

import asyncio

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agent.runner import AgentRunLauncher
from tabvis.gateway.runtime.context.runtime import ContextRuntime
from tabvis.gateway.runtime.context.sources import SourceCollector
from tabvis.gateway.runtime.orchestrator import RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore


async def _const(value):
    return value


def _collector() -> SourceCollector:
    return SourceCollector(
        project_instructions=lambda: _const("BASE INSTRUCTIONS"),
        memory=lambda: _const("BASE MEMORY"),
        git_status=lambda: _const("branch: feature-x\nclean"),
        browser_summary=lambda agent_id: {"url": "https://example.com", "title": "Example"},
    )


def test_launcher_injects_situational_context_and_emits_pack_event() -> None:
    async def scenario() -> None:
        rs = RunStore()
        captured: dict = {}

        async def capture(run, context):
            captured["system_context"] = context.extra.get("system_context")
            captured["owns"] = context.extra.get("owns_system_context")
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(
            run_store=rs, stream_fn=capture,
            context_collector=_collector(), context_runtime=ContextRuntime(),
        )
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_1", session_id="ses_1", command_id="cmd_1", model="m", prompt="go"
        )
        await launcher.join(run.run_id)

        assert rs.get_run(run.run_id).status == runs.COMPLETED

        # the pack drives the model: project instructions + memory + situational all reach the stream,
        # and the loop is told it owns project context (base prompt suppresses its own copies).
        block = captured["system_context"]
        assert block and "feature-x" in block and "example.com" in block
        assert "BASE INSTRUCTIONS" in block and "BASE MEMORY" in block
        assert captured["owns"] is True

        # a durable context.pack.built event was emitted with a digest.
        built = [e for e in get_event_store().read() if e.type == EventType.CONTEXT_PACK_BUILT]
        assert len(built) == 1
        assert built[0].data["digest"] and built[0].data["injected"] is True
        assert built[0].scope.run_id == run.run_id

    asyncio.run(scenario())


def test_launcher_without_collector_injects_nothing() -> None:
    # the default launcher (no collector) leaves the loop's own assembly untouched.
    async def scenario() -> None:
        rs = RunStore()
        captured: dict = {"system_context": "sentinel"}

        async def capture(run, context):
            captured["system_context"] = context.extra.get("system_context")
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(run_store=rs, stream_fn=capture)  # no context_collector
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go")
        await launcher.join(run.run_id)

        assert captured["system_context"] is None
        assert [e for e in get_event_store().read() if e.type == EventType.CONTEXT_PACK_BUILT] == []

    asyncio.run(scenario())


def test_context_build_failure_does_not_break_the_run() -> None:
    async def scenario() -> None:
        rs = RunStore()

        async def boom():
            raise RuntimeError("sources down")

        # a collector whose source explodes must not fail the run — context assembly is additive.
        collector = SourceCollector(project_instructions=boom, memory=lambda: _const(None),
                                    git_status=lambda: _const(None))

        async def capture(run, context):
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(run_store=rs, stream_fn=capture,
                                    context_collector=collector, context_runtime=ContextRuntime())
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go")
        await launcher.join(run.run_id)
        # the failing source degraded; the run still completed (and a pack with no situational content
        # emits an event with injected=False).
        assert rs.get_run(run.run_id).status == runs.COMPLETED

    asyncio.run(scenario())
