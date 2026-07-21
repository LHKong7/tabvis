"""Agent tools → binding_id: leased, observable browser access for a run (design §10.4).

With a Browser Runtime wired, the launcher acquires a leased binding around the run and publishes it;
a browser tool (here, a fake in the stream) resolves the active binding and drives the page through the
runtime via execute_intent — never a raw browser. Verified with a fake driver, no Chromium.
"""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.agent.runner import AgentRunLauncher
from tabvis.gateway.runtime.browser.access import active_binding, execute_intent
from tabvis.gateway.runtime.browser.contracts import BrowserAcquireRequest
from tabvis.gateway.runtime.browser.runtime import BrowserRuntime
from tabvis.gateway.runtime.orchestrator import RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore


class _FakeDriver:
    def __init__(self) -> None:
        self.launched: list[str] = []
        self.executed: list[str] = []

    async def launch(self, spec) -> None:
        self.launched.append(spec.profile_key)

    async def execute(self, profile_key: str, intent) -> dict:
        self.executed.append(intent.action)
        return {"url": intent.params.get("url"), "title": "T", "dom": "<html>ok</html>"}

    async def verify_identity(self, profile_key: str) -> bool:
        return True

    async def close(self, profile_key: str) -> None:
        pass


def test_run_acquires_a_binding_and_a_tool_drives_it() -> None:
    async def scenario() -> None:
        rs = RunStore()
        driver = _FakeDriver()
        browser = BrowserRuntime(driver=driver)
        captured: dict = {}

        async def tool_stream(run, context):
            # a "browser tool" resolves the active binding and drives the page through the runtime.
            captured["binding"] = active_binding()
            rec = await execute_intent("navigate", url="https://ex.com")
            captured["exec_status"] = rec.status
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "navigated"}]}}
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(run_store=rs, stream_fn=tool_stream, browser_runtime=browser)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go")
        await launcher.join(run.run_id)

        assert rs.get_run(run.run_id).status == runs.COMPLETED
        assert captured["binding"] and captured["binding"].startswith("bnd_")  # a binding was published
        assert captured["exec_status"] == "succeeded"
        assert driver.executed == ["navigate"]                                  # the tool drove the page

        types = [e.type for e in get_event_store().read()]
        assert EventType.BROWSER_BINDING_ACQUIRED in types
        assert EventType.BROWSER_BINDING_RELEASED in types
        # the binding was released → its profile is free for another run.
        after = await browser.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_2"))
        assert after.binding_id

    asyncio.run(scenario())


def test_binding_is_released_even_when_the_run_fails() -> None:
    async def scenario() -> None:
        rs = RunStore()
        browser = BrowserRuntime(driver=_FakeDriver())

        async def boom(run, context):
            raise RuntimeError("loop failed")
            yield  # pragma: no cover

        launcher = AgentRunLauncher(run_store=rs, stream_fn=boom, browser_runtime=browser)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go")
        await launcher.join(run.run_id)

        assert rs.get_run(run.run_id).status == runs.FAILED
        types = [e.type for e in get_event_store().read()]
        assert EventType.BROWSER_BINDING_RELEASED in types   # released despite the failure
        # profile freed: another run can take it.
        assert (await browser.acquire(BrowserAcquireRequest(agent_id="ag_1", run_id="run_2"))).binding_id

    asyncio.run(scenario())


def test_shared_profile_conflict_fails_the_run_deterministically() -> None:
    async def scenario() -> None:
        rs = RunStore()
        browser = BrowserRuntime(driver=_FakeDriver())
        # another run already holds the shared "team" profile.
        await browser.acquire(BrowserAcquireRequest(agent_id="ag_other", run_id="run_other", profile="team"))

        async def never(run, context):
            yield {"type": "result", "result": "done"}  # pragma: no cover - should not run

        launcher = AgentRunLauncher(run_store=rs, stream_fn=never, browser_runtime=browser)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(
            agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go", profile="team"
        )
        await launcher.join(run.run_id)

        final = rs.get_run(run.run_id)
        assert final.status == runs.FAILED and final.error_code == "BROWSER_PROFILE_BUSY"

    asyncio.run(scenario())


def test_execute_intent_outside_a_bound_run_raises() -> None:
    async def scenario() -> None:
        with pytest.raises(GatewayError) as ei:
            await execute_intent("snapshot")
        assert ei.value.code == "BROWSER_BINDING_NOT_FOUND"

    asyncio.run(scenario())


def test_default_launcher_acquires_no_binding() -> None:
    # without a browser_runtime the launcher leaves browser access to the loop's own manager path.
    async def scenario() -> None:
        rs = RunStore()

        async def stream(run, context):
            yield {"type": "result", "result": "done"}

        launcher = AgentRunLauncher(run_store=rs, stream_fn=stream)
        orch = RunOrchestrator(rs, launcher=launcher)
        run = await orch.create_and_start(agent_id="ag_1", session_id="ses_1", command_id="cmd_1", prompt="go")
        await launcher.join(run.run_id)
        types = [e.type for e in get_event_store().read()]
        assert EventType.BROWSER_BINDING_ACQUIRED not in types

    asyncio.run(scenario())
