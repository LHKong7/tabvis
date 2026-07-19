"""Query dependencies.

The agent loop calls the model through ``deps.call_model`` so it can be swapped in tests. The
production implementation wraps :func:`tabvis.agent.api.model_client.query_model_with_streaming`,
deriving ``Options`` from the ``ToolUseContext``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from tabvis.agent.api.model_client import Options, query_model_with_streaming
from tabvis.tool import ToolUseContext, get_empty_tool_permission_context
from tabvis.utils.system_prompt_type import SystemPrompt


@dataclass
class QueryDeps:
    call_model: Callable[..., AsyncGenerator[dict[str, Any], None]]


async def _production_call_model(
    *,
    messages: list[dict[str, Any]],
    system_prompt: SystemPrompt,
    tools: Any,
    signal: Any,
    tool_use_context: ToolUseContext,
) -> AsyncGenerator[dict[str, Any], None]:
    opts = tool_use_context.options

    async def _get_tool_permission_context() -> Any:
        app_state = tool_use_context.get_app_state() if tool_use_context.get_app_state else None
        if app_state and app_state.get("toolPermissionContext"):
            return app_state["toolPermissionContext"]
        return get_empty_tool_permission_context()

    options = Options(
        model=opts.main_loop_model,
        get_tool_permission_context=_get_tool_permission_context,
        is_non_interactive_session=opts.is_non_interactive_session,
        query_source=opts.query_source or "sdk",
        agents=[],
    )
    async for event in query_model_with_streaming(
        messages=messages,
        system_prompt=system_prompt,
        thinking_config=opts.thinking_config,
        tools=tools,
        signal=signal,
        options=options,
    ):
        yield event


production_deps = QueryDeps(call_model=_production_call_model)
