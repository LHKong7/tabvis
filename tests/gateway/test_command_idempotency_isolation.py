"""Idempotent replay must not cross principals.

``command_id`` is client-supplied (``x-tabvis-command-id`` or the request body) and both replay
short-circuits — the router's ``commands`` ledger and ``run.create``'s ``get_run_by_command`` —
run BEFORE or BESIDE the authorization that keys on the agent in the request body. Keyed on
``command_id`` alone they hand a scoped agent principal another agent's run record, prompt included.
"""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.auth.principals import agent_principal, local_admin
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.errors import GatewayError

SHARED_COMMAND_ID = "cmd_shared_0001"


class _RecordingLauncher:
    def __init__(self) -> None:
        self.launches: list[tuple[object, object]] = []

    async def launch(self, run, context) -> None:
        self.launches.append((run, context))

    async def abort(self, run_id: str) -> None:
        del run_id


def _create(gateway, principal, agent_id: str, prompt: str, command_id: str):
    return asyncio.run(
        gateway.router.dispatch(
            Command(
                type=CommandType.RUN_CREATE,
                data={
                    "agent_id": agent_id,
                    "session_id": f"ses_{agent_id}",
                    "prompt": prompt,
                },
                command_id=command_id,
            ),
            CommandContext(principal=principal),
        )
    )


def test_a_scoped_principal_cannot_replay_another_principals_command() -> None:
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    victim = _create(
        gateway, local_admin(), "ag_victim", "victim private prompt", SHARED_COMMAND_ID
    )
    assert victim.data["run"]["agent_id"] == "ag_victim"

    attacker = agent_principal("ag_attacker")
    assert attacker.can_access_agent("ag_victim") is False
    with pytest.raises(GatewayError) as exc:
        _create(gateway, attacker, "ag_attacker", "x", SHARED_COMMAND_ID)
    assert exc.value.code == "FORBIDDEN"


def test_the_issuing_principal_still_gets_its_idempotent_replay() -> None:
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    principal = agent_principal("ag_owner")
    first = _create(gateway, principal, "ag_owner", "do the thing", "cmd_owner_0001")
    second = _create(gateway, principal, "ag_owner", "do the thing", "cmd_owner_0001")

    assert second.duplicate is True
    assert second.data["run"]["run_id"] == first.data["run"]["run_id"]


def test_admin_replay_is_unchanged() -> None:
    gateway = GatewayApplication.build(launcher=_RecordingLauncher())
    first = _create(gateway, local_admin(), "ag_a", "p", "cmd_admin_0001")
    second = _create(gateway, local_admin(), "ag_a", "p", "cmd_admin_0001")

    assert second.duplicate is True
    assert second.data["run"]["run_id"] == first.data["run"]["run_id"]
