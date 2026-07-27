"""The event SSE stream must respect ownership isolation.

``GET /v1/events`` authenticates and then replays the durable log. ``run_id`` is a client-supplied
filter, not an authorization boundary, so without a principal filter any scoped agent credential
reads the entire global log — every other agent's runs and prompts.
"""

from __future__ import annotations

import asyncio
import json

from tabvis.gateway.access.sse import event_stream
from tabvis.gateway.auth.principals import agent_principal, local_admin
from tabvis.gateway.lifecycle import GatewayApplication


class _RecordingLauncher:
    async def launch(self, run, context) -> None:
        del run, context

    async def abort(self, run_id: str) -> None:
        del run_id


def _gateway() -> GatewayApplication:
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    gateway.runs.create_run(
        agent_id="ag_victim", session_id="s1", command_id="c1", prompt="victim secret"
    )
    gateway.runs.create_run(
        agent_id="ag_other", session_id="s2", command_id="c2", prompt="other"
    )
    return gateway


def _agents_seen(gateway: GatewayApplication, principal) -> set[str | None]:
    async def collect() -> list[dict]:
        return [
            frame
            async for frame in event_stream(
                gateway.events, follow=False, principal=principal
            )
        ]

    return {
        json.loads(frame["data"])["scope"].get("agent_id")
        for frame in asyncio.run(collect())
    }


def test_an_agent_principal_sees_only_its_own_events() -> None:
    gateway = _gateway()
    assert _agents_seen(gateway, agent_principal("ag_other")) == {"ag_other"}


def test_an_admin_still_sees_everything() -> None:
    gateway = _gateway()
    assert _agents_seen(gateway, local_admin()) == {"ag_victim", "ag_other"}


def test_no_principal_means_the_caller_already_scoped_the_read() -> None:
    gateway = _gateway()
    assert _agents_seen(gateway, None) == {"ag_victim", "ag_other"}
