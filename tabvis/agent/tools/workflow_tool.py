"""Workflow tool — the model-invokable entry point for dynamic workflows.

The model calls ``Workflow`` with an inline **Python** orchestration ``script`` (and optional
structured ``args``). The tool extracts the script's literal ``meta`` (for the approval card),
hands the script to :func:`tabvis.agent.workflows.run.run_workflow`, and returns **only the workflow's
final result** as the ``tool_result`` — the dozens-to-hundreds of intermediate sub-agent outputs
stay inside the workflow's own task transcript and never enter the main session's context.

This realizes the Dynamic Workflows PRD's primary trigger (the model writes a script and runs it):
  * **G1** script + orchestration API (``agent`` / ``parallel`` / ``phase`` / ``log`` / ``args``),
  * **G2** background execution (the run registers a ``local_workflow`` task with disk output +
    per-phase progress),
  * **G4** pre-run approval — :meth:`check_permissions` returns ``ask`` interactively (the approval
    card shows the workflow name/description) and ``allow`` in non-interactive/SDK mode (the PRD's
    "``-p`` / SDK mode runs immediately, no prompts"),
  * **G7** structured ``args`` pass-through,
  * **G9** resource caps (concurrency ≤ 16, ≤ 1000 total agents) are enforced by the runner.

Security (PRD §9 / S-1): the script has **no** direct filesystem or shell access — every side effect
goes through ``agent`` (a sandboxed sub-agent). The script is AST-validated + sandbox-executed by
:mod:`tabvis.agent.workflows.engine`.

Casing: Python identifiers are snake_case; the result ``data`` dict and ``tool_result`` block keep
their wire keys (``content`` / ``taskId`` / ``tool_use_id``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.types.message import AssistantMessage
from tabvis.types.permissions import PermissionResult
from tabvis.utils.errors import get_error_message
from tabvis.agent.workflows.engine import extract_workflow_meta
from tabvis.agent.workflows.run import run_workflow

WORKFLOW_TOOL_NAME = "Workflow"


class WorkflowToolInput(BaseModel):
    """Validated input for :data:`workflow_tool`."""

    model_config = ConfigDict(extra="forbid")

    script: str = Field(
        description=(
            "The Python workflow orchestration script. It must declare a literal "
            "`meta = {\"name\": \"...\", \"description\": \"...\"}` at the top and `return` its "
            "final result. In scope: `await agent(prompt_or_input)`, `await parallel([thunks])`, "
            "`phase(name)`, `log(message)`, `args`, `meta`, `gather`, `pipeline`. No imports and no "
            "direct filesystem/shell access — all side effects go through agent()."
        )
    )
    args: Any = Field(
        default=None,
        description=(
            "Optional structured arguments exposed to the script as the `args` global (a string, "
            "list, or object — passed through verbatim)."
        ),
    )
    name: str | None = Field(
        default=None,
        description="Optional workflow name override (otherwise read from the script's meta).",
    )
    description: str | None = Field(
        default=None,
        description="Optional workflow description override (otherwise read from the script's meta).",
    )
    resume_task_id: str | None = Field(
        default=None,
        description=(
            "Optional task id of a prior (killed/failed) run of this same workflow. When set, "
            "sub-agent results already recorded by that run are replayed instead of re-spawned, so "
            "the workflow continues from where it stopped."
        ),
    )


_TOOL_PROMPT = """Run a dynamic workflow: a background script that orchestrates many sub-agents and \
returns only its final result to you.

Use this when a task decomposes into many independent or staged sub-tasks (review every file, \
research several angles, migrate many call-sites) and you want the orchestration — loops, branches, \
intermediate results — to run as one bounded background job instead of cluttering the main \
conversation. Only the script's `return` value comes back to you; every sub-agent's output stays in \
the workflow's own transcript.

The `script` is **Python**. It must:
- declare a literal `meta = {"name": "short-kebab-name", "description": "one sentence"}` at the top \
(read without executing the body, for the approval card), and
- `return` its final result (a string, or a dict — a `{"summary": "..."}` dict is summarized for you).

In scope (provided by the runtime — do NOT import anything):
- `args` — the structured arguments you passed in.
- `await agent(prompt)` or `await agent({"prompt": ..., "name"?: ..., "agentType"?: ..., \
"model"?: ..., "allowedTools"?: [...], "maxTurns"?: N})` — spawn one sub-agent; returns \
`{"name", "result", "totalTokens", "toolUses", "durationMs"}`. Sub-agents may read, edit, and run \
shell commands as needed.
- `await parallel([lambda: agent(...), ...])` — run a list of zero-arg thunks concurrently \
(concurrency-capped); returns their results in order.
- `await pipeline(items, stage1, stage2, ...)` — run each item through every stage independently.
- `phase(name)` — start a named progress phase. `log(message)` — emit a progress line.
- `gather` — `asyncio.gather`, for awaiting coroutines together.

Rules:
- No `import`, `open`, `eval`, `exec`, `process`, `fs`, or shell APIs in the script — orchestration \
only; all side effects go through agent().
- Keep it bounded: at most 16 sub-agents run concurrently and at most 1000 run in total.

Example:
    meta = {"name": "review-files", "description": "Review each file for missing error handling"}

    phase("review")
    reviews = await parallel([(lambda f=f: agent(f"Review {f} for missing error handling")) \
for f in args])
    return {"summary": f"Reviewed {len(reviews)} files",
            "findings": [r["result"] for r in reviews]}"""


class WorkflowTool(Tool):
    """``Workflow`` — run a model-authored Python orchestration script in the background."""

    name = WORKFLOW_TOOL_NAME
    search_hint = "orchestrate many sub-agents from a script"
    input_schema = WorkflowToolInput
    max_result_size_chars = 100_000

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return "Run a dynamic multi-agent workflow"

    async def prompt(self, options: dict[str, Any]) -> str:
        return _TOOL_PROMPT

    def is_read_only(self, input: Any) -> bool:
        # Like the Agent tool: the workflow itself touches nothing; its sub-agents carry their own
        # permission checks.
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        # A workflow drives the shared agent-slot scheduler + app state; serialize against other
        # tool calls to keep the concurrency accounting and task store coherent.
        return False

    def get_activity_description(self, input: Any | None) -> str | None:
        if input is None:
            return "Running workflow"
        name = (
            getattr(input, "name", None)
            if not isinstance(input, dict)
            else input.get("name")
        )
        return f"Running workflow: {name}" if name else "Running workflow"

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionResult:
        # Pre-run approval gate (G4). Interactive sessions surface the workflow's meta as an approval
        # card; non-interactive / SDK runs proceed immediately (the PRD's "-p / SDK mode, no prompts").
        if context.options.is_non_interactive_session:
            return {"behavior": "allow", "updatedInput": input}
        meta = self._resolve_meta(input)
        message = f"Run workflow \"{meta['name']}\""
        if meta.get("description"):
            message += f": {meta['description']}"
        message += "?"
        return {"behavior": "ask", "message": message, "updatedInput": input}

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[Any]:
        script = args.script
        meta = self._resolve_meta(args)
        resume_task_id = getattr(args, "resume_task_id", None)
        try:
            result = await run_workflow(
                args=getattr(args, "args", None),
                script=script,
                script_path="<inline-workflow>",
                meta=meta,
                context=context,
                can_use_tool=can_use_tool,
                task_id=resume_task_id or None,
                resume=bool(resume_task_id),
            )
        except Exception as error:  # noqa: BLE001 - surface a clean tool_result to the model.
            return ToolResult(
                data={
                    "content": f"Workflow failed: {get_error_message(error)}",
                    "workflowName": meta["name"],
                    "error": True,
                }
            )
        return ToolResult(
            data={
                "content": result["result"],
                "taskId": result["taskId"],
                "workflowName": result["workflowName"],
                "totalTokens": result["totalTokens"],
                "toolUses": result["toolUses"],
                "durationMs": result["durationMs"],
            }
        )

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        text = data.get("content") or "(Workflow completed but returned no result.)"
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": text,
        }

    @staticmethod
    def _resolve_meta(input: Any) -> dict[str, Any]:
        """Read the script's literal ``meta``, applying ``name`` / ``description`` overrides."""
        script = getattr(input, "script", None)
        if script is None and isinstance(input, dict):
            script = input.get("script")
        meta: dict[str, Any] = dict(extract_workflow_meta(script or "", "workflow"))
        name_override = (
            getattr(input, "name", None) if not isinstance(input, dict) else input.get("name")
        )
        desc_override = (
            getattr(input, "description", None)
            if not isinstance(input, dict)
            else input.get("description")
        )
        if name_override:
            meta["name"] = name_override
        if desc_override:
            meta["description"] = desc_override
        return meta


workflow_tool = WorkflowTool()
