"""Cancelling a Bash tool call must reap the whole command, not detach it.

``_run_shell_command`` spawns with ``start_new_session=True`` so a timeout can reap the process
group — which also means the command does NOT die with the agent. ``asyncio.CancelledError`` is a
``BaseException``, so an ``except TimeoutError`` branch never sees an abort, and the entire
``bash -c`` tree (a dev server, a test run) would keep running detached for the machine's uptime.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from tabvis.agent.tools.bash_tool import _run_shell_command
from tabvis.tool import ToolUseContext, ToolUseContextOptions

MARKER = "tabvis-reap-test-6f21c0"


def _survivors() -> list[int]:
    listing = subprocess.run(
        ["ps", "-eo", "pid,args"], capture_output=True, text=True
    ).stdout
    return [
        int(line.split()[0])
        for line in listing.splitlines()
        if MARKER in line and "ps -eo" not in line
    ]


@pytest.fixture(autouse=True)
def _no_stragglers():
    yield
    for pid in _survivors():
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def test_cancelling_the_call_kills_the_command_and_its_children() -> None:
    context = ToolUseContext(
        options=ToolUseContextOptions(tools=[], main_loop_model="m"), messages=[]
    )
    # A backgrounded grandchild plus the foreground sleep: killing only the bash PID leaves both.
    command = f"sh -c 'sleep 300 # {MARKER}' & sleep 300 # {MARKER}"

    async def scenario() -> int:
        task = asyncio.ensure_future(_run_shell_command(command, 300_000, context))
        await asyncio.sleep(1.0)
        assert _survivors(), "the command should be running before we cancel it"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.8)
        return len(_survivors())

    assert asyncio.run(scenario()) == 0
