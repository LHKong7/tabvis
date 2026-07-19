"""Tool execution.

Happy path for ``run_tool_use``: find tool (alias fallback) → validate input
(``model_validate``) → ``validate_input`` → permission (``can_use_tool``) → ``tool.call`` → map
result via ``map_tool_result_to_tool_result_block_param`` → ``tool_result`` user message. Yields
``MessageUpdateLazy`` = ``{'message', 'contextModifier'?}``.

Not supported in this build: analytics/OTel spans, Pre/PostToolUse hook handling,
MCP-tool result processing, structured-output attachments, image/accept-feedback content blocks,
and the deferred-tool schema hint.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator
from typing import Any

from pydantic import ValidationError

from tabvis.tool import Tool, ToolUseContext, find_tool_by_name
from tabvis.agent.tools import get_all_base_tools
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.utils.log import log_error
from tabvis.utils.messages import create_user_message
from tabvis.utils.tool_errors import format_pydantic_validation_error

CANCEL_MESSAGE = (
    "The user doesn't want to take this action right now. STOP what you are doing and wait for "
    "the user to tell you how to proceed."
)

# MessageUpdateLazy = {'message': Message, 'contextModifier'?: {'toolUseID', 'modifyContext'}}
MessageUpdateLazy = dict[str, Any]


def _tool_result_error_update(
    tool_use_id: str, content: str, tool_use_result: str, assistant_message: dict[str, Any]
) -> MessageUpdateLazy:
    return {
        "message": create_user_message(
            content=[
                {
                    "type": "tool_result",
                    "content": content,
                    "is_error": True,
                    "tool_use_id": tool_use_id,
                }
            ],
            tool_use_result=tool_use_result,
            source_tool_assistant_uuid=assistant_message.get("uuid"),
        )
    }


async def run_tool_use(
    tool_use: dict[str, Any],
    assistant_message: dict[str, Any],
    can_use_tool: CanUseToolFn,
    tool_use_context: ToolUseContext,
) -> AsyncGenerator[MessageUpdateLazy, None]:
    tool_name = tool_use["name"]
    tool = find_tool_by_name(tool_use_context.options.tools, tool_name)

    # Deprecated-alias fallback: only accept a base tool found via an alias (not primary name).
    if tool is None:
        fallback = find_tool_by_name(get_all_base_tools(), tool_name)
        if fallback is not None and tool_name in (fallback.aliases or []):
            tool = fallback

    if tool is None:
        yield _tool_result_error_update(
            tool_use["id"],
            f"<tool_use_error>Error: No such tool available: {tool_name}</tool_use_error>",
            f"Error: No such tool available: {tool_name}",
            assistant_message,
        )
        return

    try:
        if tool_use_context.abort_controller.signal.aborted:
            yield _tool_result_error_update(
                tool_use["id"], CANCEL_MESSAGE, CANCEL_MESSAGE, assistant_message
            )
            return
        for update in await _check_permissions_and_call_tool(
            tool, tool_use["id"], tool_use["input"], tool_use_context, can_use_tool, assistant_message
        ):
            yield update
    except Exception as error:  # noqa: BLE001 - catch-all -> tool_result error
        log_error(error)
        detailed = f"Error calling tool ({tool.name}): {error}"
        yield _tool_result_error_update(
            tool_use["id"],
            f"<tool_use_error>{detailed}</tool_use_error>",
            detailed,
            assistant_message,
        )


async def _check_permissions_and_call_tool(
    tool: Tool,
    tool_use_id: str,
    input: dict[str, Any],
    context: ToolUseContext,
    can_use_tool: CanUseToolFn,
    assistant_message: dict[str, Any],
) -> list[MessageUpdateLazy]:
    # 1. Validate input shape with pydantic.
    try:
        validated = tool.input_schema.model_validate(input)
    except ValidationError as error:
        error_content = format_pydantic_validation_error(tool.name, error)
        return [
            _tool_result_error_update(
                tool_use_id,
                f"<tool_use_error>InputValidationError: {error_content}</tool_use_error>",
                f"InputValidationError: {error}",
                assistant_message,
            )
        ]

    # 2. Tool-specific input validation.
    valid = await tool.validate_input(validated, context)
    if valid is not None and valid.result is False:
        return [
            _tool_result_error_update(
                tool_use_id,
                f"<tool_use_error>{valid.message}</tool_use_error>",
                f"Error: {valid.message}",
                assistant_message,
            )
        ]

    # 3. Permission decision. Hook execution is not part of this runtime, so the normal
    # permission resolver is the single source of truth.
    decision = await can_use_tool(tool, validated, context, assistant_message, tool_use_id)
    if decision.get("behavior") != "allow":
        message = decision.get("message", "")
        return [
            _tool_result_error_update(
                tool_use_id, message, f"Error: {message}", assistant_message
            )
        ]

    call_input = decision.get("updatedInput", validated)
    if isinstance(call_input, dict):  # a permission resolver returned a fresh dict -> re-validate
        call_input = tool.input_schema.model_validate(call_input)

    call_context = dataclasses.replace(
        context, tool_use_id=tool_use_id, user_modified=bool(decision.get("userModified", False))
    )

    # 4. Run the tool.
    result = await tool.call(call_input, call_context, can_use_tool, assistant_message, None)

    updates: list[MessageUpdateLazy] = []
    for new_message in result.new_messages or []:
        updates.append({"message": new_message})

    block = tool.map_tool_result_to_tool_result_block_param(result.data, tool_use_id)
    updates.append(
        {
            "message": create_user_message(
                content=[block],
                tool_use_result=result.data,
                mcp_meta=result.mcp_meta,
                source_tool_assistant_uuid=assistant_message.get("uuid"),
            ),
            "contextModifier": (
                {"toolUseID": tool_use_id, "modifyContext": result.context_modifier}
                if result.context_modifier
                else None
            ),
        }
    )
    return updates
