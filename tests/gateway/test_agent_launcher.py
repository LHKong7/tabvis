"""Wiring the RunLauncher to the agent loop (design §7, §7.8).

The real loop (`stream_agent`) needs a model and a browser, so these tests inject a fake stream — the
same seam the launcher uses in production, just swapped for canned messages — and assert the launcher
drives the Run through the real state machine and emits the right events.
"""

from __future__ import annotations

import asyncio

from tabvis.gateway.access.http import create_gateway_app
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agent.runner import AgentRunLauncher
from tabvis.gateway.runtime.orchestrator import RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore


def _assistant(text: str, tool_uses: int = 0) -> dict:
    content = [{"type": "text", "text": text}]
    content += [
        {
            "type": "tool_use",
            "name": "BrowserExtract",
            "id": f"t{i}",
            "input": {"url": "https://example.test/document-1991237455038119936"},
        }
        for i in range(tool_uses)
    ]
    return {"type": "assistant", "message": {"content": content}}


def _result(text: str, is_error: bool = False) -> dict:
    return {"type": "result", "result": text, "is_error": is_error}


def _fake_stream(messages):
    async def stream(run, context):
        for m in messages:
            yield m

    return stream


def _launcher(messages, run_store=None) -> AgentRunLauncher:
    return AgentRunLauncher(run_store=run_store, stream_fn=_fake_stream(messages))


def test_launch_drives_run_to_completed_with_counters() -> None:
    async def scenario() -> None:
        rs = RunStore()
        launcher = _launcher([_assistant("working", tool_uses=2), _assistant("done"), _result("all set")], rs)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="do it")
        await launcher.join(run.run_id)

        final = rs.get_run(run.run_id)
        assert final.status == runs.COMPLETED
        assert final.turns == 2 and final.tool_calls == 2

        types = [e.type for e in get_event_store().read(aggregate_id=run.run_id)]
        assert types[:3] == ["run.created", "run.preparing", "run.started"]
        assert "assistant.message.completed" in types
        assert "tool.completed" in types
        assert types[-1] == "run.completed"
        tool_event = next(
            e for e in get_event_store().read(aggregate_id=run.run_id)
            if e.type == "tool.completed"
        )
        assert tool_event.data["name"] == "BrowserExtract"
        assert tool_event.data["input"]["url"].endswith("1991237455038119936")

    asyncio.run(scenario())


def test_running_run_persists_live_counters_before_terminal() -> None:
    async def scenario() -> None:
        rs = RunStore()
        observed = asyncio.Event()
        release = asyncio.Event()

        async def stream(run, context):
            yield _assistant("working", tool_uses=2)
            observed.set()
            await release.wait()
            yield _result("done")

        launcher = AgentRunLauncher(run_store=rs, stream_fn=stream)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_live", session_id="ses_live", command_id="cmd_live", prompt="x"
        )
        await observed.wait()
        await asyncio.sleep(0)
        current = rs.get_run(run.run_id)
        assert current.status == runs.RUNNING
        assert current.turns == 1 and current.tool_calls == 2
        release.set()
        await launcher.join(run.run_id)

    asyncio.run(scenario())


def test_web_permission_gate_pauses_for_ask_user_question_and_resumes() -> None:
    async def scenario() -> None:
        from tabvis.agent.tools.ask_user_question_tool import (
            AskUserQuestionInput,
            ask_user_question_tool,
        )
        from tabvis.gateway.runtime.interaction_service import InteractionService
        from tabvis.tool import ToolUseContext

        rs = RunStore()
        run = rs.create_run(
            agent_id="ag_question",
            session_id="ses_question",
            command_id="cmd_question",
        )
        rs.transition(run.run_id, runs.PREPARING)
        rs.transition(run.run_id, runs.RUNNING)
        launcher = AgentRunLauncher(run_store=rs)
        gate = launcher._interactive_can_use_tool(run)
        tool_input = AskUserQuestionInput.model_validate(
            {
                "questions": [
                    {
                        "question": "Which environment?",
                        "header": "Environment",
                        "options": [
                            {"label": "Production", "description": "Use production"},
                            {"label": "Staging", "description": "Use staging"},
                        ],
                    }
                ]
            }
        )

        task = asyncio.create_task(
            gate(
                ask_user_question_tool,
                tool_input,
                ToolUseContext(),
                {"type": "assistant"},
                "tool_1",
            )
        )
        service = InteractionService(run_store=rs)
        for _ in range(20):
            pending = service.list_pending()
            if pending:
                break
            await asyncio.sleep(0)
        assert pending and rs.get_run(run.run_id).status == runs.WAITING_FOR_INPUT
        service.respond(
            pending[0].interaction_id,
            {"Which environment?": "Staging"},
            response_command_id="cmd_answer",
        )
        decision = await task
        assert decision["behavior"] == "allow"
        assert decision["updatedInput"]["answers"] == {
            "Which environment?": "Staging"
        }
        assert rs.get_run(run.run_id).status == runs.RUNNING

    asyncio.run(scenario())


def test_launch_records_a_failed_run_on_error_result() -> None:
    async def scenario() -> None:
        rs = RunStore()
        launcher = _launcher([_assistant("trying"), _result("nope", is_error=True)], rs)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="x")
        await launcher.join(run.run_id)
        final = rs.get_run(run.run_id)
        assert final.status == runs.FAILED and final.error_code == "agent_error"

    asyncio.run(scenario())


def test_model_timeout_retry_is_visible_and_preserves_specific_error_code() -> None:
    async def scenario() -> None:
        rs = RunStore()
        messages = [
            {
                "type": "model_retry",
                "reason": "model_stream_timeout",
                "retry_attempt": 1,
                "max_retries": 1,
                "retry_in_ms": 500,
            },
            _assistant("Model response timed out."),
            {
                "type": "result",
                "result": "Model response timed out.",
                "is_error": True,
                "error_code": "model_stream_timeout",
            },
        ]
        launcher = _launcher(messages, rs)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_timeout",
            session_id="ses_timeout",
            command_id="cmd_timeout",
            prompt="continue",
        )
        await launcher.join(run.run_id)

        final = rs.get_run(run.run_id)
        assert final.status == runs.FAILED
        assert final.error_code == "model_stream_timeout"
        events = get_event_store().read(aggregate_id=run.run_id)
        retry = next(event for event in events if event.type == "run.retrying")
        assert retry.data["retry_attempt"] == 1
        assert "retrying 1/1" in retry.data["message"]
        assert any(event.type == "run.resumed" for event in events)

    asyncio.run(scenario())


def test_retrying_run_can_be_cancelled() -> None:
    async def scenario() -> None:
        rs = RunStore()
        retrying = asyncio.Event()
        release = asyncio.Event()

        async def stream(_run, _context):
            yield {
                "type": "model_retry",
                "reason": "model_stream_timeout",
                "retry_attempt": 1,
                "max_retries": 1,
                "retry_in_ms": 500,
            }
            retrying.set()
            await release.wait()

        launcher = AgentRunLauncher(run_store=rs, stream_fn=stream)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_retry_cancel",
            session_id="ses_retry_cancel",
            command_id="cmd_retry_cancel",
            prompt="continue",
        )
        await retrying.wait()
        await asyncio.sleep(0)
        assert rs.get_run(run.run_id).status == runs.RETRYING
        cancelled = await orch.cancel(run.run_id)
        assert cancelled.status == runs.CANCELLED

    asyncio.run(scenario())


def test_launch_records_failed_on_exception_in_the_loop() -> None:
    async def scenario() -> None:
        rs = RunStore()

        async def boom(run, context):
            raise RuntimeError("model exploded")
            yield  # pragma: no cover - makes this an async generator

        launcher = AgentRunLauncher(run_store=rs, stream_fn=boom)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="x")
        await launcher.join(run.run_id)
        final = rs.get_run(run.run_id)
        assert final.status == runs.FAILED and final.error_code == "agent_exception"

    asyncio.run(scenario())


def test_no_secret_or_full_content_dumped_bounds_assistant_text() -> None:
    async def scenario() -> None:
        rs = RunStore()
        huge = "x" * 5000
        launcher = _launcher([_assistant(huge), _result("ok")], rs)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="x")
        await launcher.join(run.run_id)
        msg = next(e for e in get_event_store().read(aggregate_id=run.run_id)
                   if e.type == "assistant.message.completed")
        assert len(msg.data["text_preview"]) <= 2000  # bounded, never the full 5000 chars

    asyncio.run(scenario())


def test_abort_cancels_a_running_launch() -> None:
    async def scenario() -> None:
        rs = RunStore()
        started = asyncio.Event()

        async def slow_stream(run, context):
            started.set()
            yield _assistant("thinking")
            await asyncio.sleep(10)  # would run forever without an abort
            yield _result("never")

        launcher = AgentRunLauncher(run_store=rs, stream_fn=slow_stream)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="x")
        await started.wait()
        # cancel via the orchestrator — it owns the run.cancelling → cancelled transitions.
        cancelled = await orch.cancel(run.run_id)
        assert cancelled.status == runs.CANCELLED

    asyncio.run(scenario())


def test_end_to_end_run_executes_over_http() -> None:
    # A POST /v1/runs with a wired launcher actually runs the (fake) agent to completion.
    async def scenario() -> None:
        # No explicit run store → launcher and the built gateway share the singleton store.
        launcher = AgentRunLauncher(stream_fn=_fake_stream([_assistant("hi"), _result("done")]))
        gw = GatewayApplication.build(launcher=launcher)
        assert gw.health()["components"]["agent_runtime"] == "ready"

        from starlette.testclient import TestClient

        client = TestClient(create_gateway_app(gw))
        run = client.post("/v1/runs", json={"message": {"text": "hello"}}).json()["data"]["run"]
        await launcher.join(run["run_id"])
        assert gw.runs.get_run(run["run_id"]).status == runs.COMPLETED

    asyncio.run(scenario())
