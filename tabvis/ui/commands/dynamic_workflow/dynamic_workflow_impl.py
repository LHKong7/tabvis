"""``/dynamic-workflow`` local command implementation.

COLLISION RULE: the package ``__init__`` exports the ``dynamic_workflow`` command singleton, which
would shadow a ``dynamic_workflow.py`` submodule, so the implementation lives in
``dynamic_workflow_impl.py`` (matching the ``compact_impl`` / ``cost_impl`` precedent).

``call(args, context)`` generates a workflow script for the task, saves it as a personal workflow,
runs it via :func:`tabvis.agent.workflows.run.run_workflow`, and returns a text summary (workflow name /
task id / script path / result). On any error it returns a ``{type:'text'}`` result with the error
message (TS ``errorMessage`` → :func:`tabvis.utils.errors.get_error_message`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tabvis.utils.errors import get_error_message
from tabvis.agent.workflows.generate import generate_workflow_script
from tabvis.agent.workflows.storage import save_personal_workflow

if TYPE_CHECKING:
    from tabvis.tool import ToolUseContext
    from tabvis.types.command import LocalCommandResult

__all__ = ["call"]


async def call(args: str, context: ToolUseContext) -> LocalCommandResult:
    """Generate, save, and run a workflow."""
    task = args.strip()
    if not task:
        return {"type": "text", "value": "Usage: /dynamic-workflow <task>"}

    try:
        from tabvis.agent.workflows.run import run_workflow

        workflow = await generate_workflow_script(task, context)
        script_path = await save_personal_workflow(workflow)
        result = await run_workflow(
            args=task,
            script=workflow["script"],
            script_path=script_path,
            meta=workflow["meta"],
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
    except Exception as error:  # noqa: BLE001 - faithful translation of the TS try/catch.
        return {
            "type": "text",
            "value": f"Dynamic workflow failed: {get_error_message(error)}",
        }
