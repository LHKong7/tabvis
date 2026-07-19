"""Workflow script generation

Asks the model to produce a JSON ``{meta, script}`` workflow definition for a task, parses the
(possibly fenced) JSON out of the response, then normalizes the meta + validates the script.

Workflow scripts are **Python** (tabvis has no embedded JS engine — see :mod:`tabvis.agent.workflows.engine`),
so the generation prompt asks for Python source and the script is validated by the Python sandbox
validator. ``JSON.parse`` -> :func:`json.loads` (with the same ``{`` .. ``}`` slice fallback). The
model call (:func:`tabvis.agent.api.model_client.query_with_model`) and the assistant-text
extraction (:func:`tabvis.utils.messages.get_assistant_message_text`) are imported **lazily**
(function-local) so this module imports standalone. ``WorkflowMeta`` / ``GeneratedWorkflow`` keep
their wire keys (``name`` / ``description`` / ``script``).
"""

from __future__ import annotations

import json
from typing import Any

from tabvis.tool import ToolUseContext
from tabvis.utils.system_prompt_type import as_system_prompt
from tabvis.agent.workflows.engine import validate_python_workflow
from tabvis.agent.workflows.script import normalize_workflow_meta
from tabvis.agent.workflows.types import GeneratedWorkflow


def _extract_json_object(text: str) -> Any:
    """``extractJsonObject`` — parse JSON, else slice the outermost ``{`` .. ``}`` and retry."""
    trimmed = text.strip()
    try:
        return json.loads(trimmed)
    except (ValueError, json.JSONDecodeError):
        start = trimmed.find("{")
        end = trimmed.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found") from None
        return json.loads(trimmed[start : end + 1])


async def generate_workflow_script(task: str, context: ToolUseContext) -> GeneratedWorkflow:
    """Generate a workflow script for ``task`` via the model."""
    prompt = f"""Create a dynamic workflow script for this task:

{task}

Return ONLY a JSON object with this exact shape:
{{
  "meta": {{ "name": "short-kebab-name", "description": "one sentence" }},
  "script": "Python source code"
}}

The script is Python. It must declare a literal `meta` and `return` its final result:
meta = {{"name": "...", "description": "..."}}
phase("scan")
results = await parallel([(lambda x=x: agent({{"prompt": f"work on {{x}}", "name": "worker"}})) for x in args])
log(f"did {{len(results)}} items")
return {{"summary": "final report"}}

In scope (provided — do NOT import anything): args, await agent(prompt_or_dict), \
await parallel([thunks]), await pipeline(items, *stages), phase(name), log(message), gather.
agent input dict: {{"prompt": ..., "name"?: ..., "agentType"?: ..., "model"?: ..., \
"allowedTools"?: [...], "maxTurns"?: N}}; it returns {{"name", "result", "totalTokens", "toolUses"}}.

Rules:
- Do not import modules. Do not use open, exec, eval, process, fs, child_process, or shell APIs.
- The workflow script coordinates agents only. Agents may read, edit, and run shell commands as needed.
- Keep the workflow focused and bounded (at most 16 agents run at once, 1000 in total)."""

    # Lazy-import keeps this module import-standalone; both are now implemented (non-streaming model call
    # + assistant-text extraction) with the TS call surface (systemPrompt/userPrompt/signal/options).
    from tabvis.agent.api.model_client import query_with_model
    from tabvis.utils.messages import get_assistant_message_text

    options = context.options
    response = await query_with_model(
        {
            "systemPrompt": as_system_prompt(
                [
                    "You write safe, bounded Python workflow orchestration scripts for Tabvis. "
                    "Return only valid JSON.",
                ]
            ),
            "userPrompt": prompt,
            "signal": context.abort_controller.signal,
            "options": {
                "model": options.main_loop_model,
                "isNonInteractiveSession": options.is_non_interactive_session,
                "querySource": getattr(options, "query_source", None) or "repl_main_thread",
                "agents": options.agent_definitions["activeAgents"],
                "hasAppendSystemPrompt": bool(getattr(options, "append_system_prompt", None)),
                "mcpTools": options.tools,
                "enablePromptCaching": False,
            },
        }
    )

    text = get_assistant_message_text(response) or ""
    if not text.strip():
        raise ValueError(
            "Model did not return workflow JSON. Check model and auth configuration."
        )
    try:
        parsed: dict[str, Any] = _extract_json_object(text)
    except Exception as error:  # noqa: BLE001
        raise ValueError(
            "Model did not return valid workflow JSON. Check model and auth configuration."
        ) from error
    if not isinstance(parsed.get("script"), str):
        raise ValueError("Generated workflow response did not include a script string")

    meta = normalize_workflow_meta(parsed.get("meta"))
    validate_python_workflow(parsed["script"])
    return {"meta": meta, "script": parsed["script"]}
