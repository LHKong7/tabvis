"""Workflow → command adapter

:func:`workflow_spec_to_command` turns a :data:`WorkflowCommandSpec` into a :class:`LocalCommand`
whose lazy ``load().call(args, context)`` reads the workflow script from disk, runs it via
:func:`tabvis.agent.workflows.run.run_workflow`, and returns the formatted text result.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tabvis.tool import ToolUseContext
from tabvis.types.command import Command, LocalCommand, LocalCommandModule
from tabvis.agent.workflows.run import run_workflow
from tabvis.agent.workflows.types import WorkflowCommandSpec


def workflow_spec_to_command(spec: WorkflowCommandSpec) -> Command:
    """Build a ``local`` workflow-backed command from ``spec``."""

    async def call(args: str, context: ToolUseContext) -> dict[str, Any]:
        script = await asyncio.to_thread(_read_script, spec["scriptPath"])
        result = await run_workflow(
            args=args,
            script=script,
            script_path=spec["scriptPath"],
            meta=spec["meta"],
            context=context,
        )
        return {
            "type": "text",
            "value": "\n".join(
                [
                    f"Workflow: {result['workflowName']}",
                    f"Task ID: {result['taskId']}",
                    f"Script: {result['scriptPath']}",
                    "",
                    result["result"],
                ]
            ),
        }

    async def load() -> LocalCommandModule:
        return {"call": call}

    return LocalCommand(
        name=spec["name"],
        description=spec["description"],
        supports_non_interactive=True,
        kind="workflow",
        loaded_from="commands_DEPRECATED" if spec["source"] == "project" else "skills",
        load=load,
    )


def _read_script(script_path: str) -> str:
    """``readFile(path, 'utf8')`` equivalent (run off-thread to mirror ``node:fs/promises``)."""
    with open(script_path, encoding="utf-8") as fh:
        return fh.read()
