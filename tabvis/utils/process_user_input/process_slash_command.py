"""Process a ``/``-prefixed slash command.

Parses the slash command, resolves it against the registered commands, and routes to the local
(imperative) or prompt (skill) handler — building the user/command-input/attachment messages and the
``{messages, shouldQuery, ...}`` result the query loop consumes. Skill expansions register their
frontmatter hooks, record usage, and stamp ``addInvokedSkill``.

Several message helpers (synthetic user-caveat message, user-interruption message, command-input
message, command-input-tag formatting, and user-content preparation) are not part of the
``tabvis.utils.messages`` surface; each is implemented locally in this module via a function-local
lazy import with a self-contained fallback.

Casing: Python identifiers snake_case; the result dicts keep their wire keys (``shouldQuery`` /
``allowedTools`` / ``resultText`` / ``isMeta`` …). The :class:`Command` dataclasses already use
snake_case attrs (``user_invocable`` / ``is_sensitive`` / ``get_prompt_for_command`` / ``skill_root``
/ ``allowed_tools`` / ``loaded_from`` / ``progress_message``).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from tabvis.constants.messages import NO_CONTENT_MESSAGE
from tabvis.constants.xml import COMMAND_MESSAGE_TAG, COMMAND_NAME_TAG
from tabvis.tool import ToolUseContext
from tabvis.types.command import Command, get_command_name
from tabvis.utils.agent_context import get_agent_context
from tabvis.utils.generators import to_array
from tabvis.utils.log import log_error
from tabvis.utils.messages import create_user_message
from tabvis.utils.slash_command_parsing import parse_slash_command
from tabvis.utils.suggestions.skill_usage_tracking import record_skill_usage

ContentBlockParam = dict[str, Any]
ProcessUserInputContext = Any


def _malformed_command_error() -> type[Exception]:
    """``MalformedCommandError`` lives in ``prompt_shell_execution`` (errors.ts split)."""
    from tabvis.utils.prompt_shell_execution import MalformedCommandError

    return MalformedCommandError


async def process_slash_command(
    input_string: str,
    preceding_input_blocks: list[ContentBlockParam],
    image_content_blocks: list[ContentBlockParam],
    attachment_messages: list[dict[str, Any]],
    context: ProcessUserInputContext,
    _set_tool_jsx: Any = None,
    uuid: str | None = None,
) -> dict[str, Any]:
    """Parse + dispatch a ``/``-prefixed command."""
    parsed = parse_slash_command(input_string)
    if not parsed:
        raise _malformed_command_error()(f"Invalid slash command: {input_string}")
    command_name = parsed.command_name
    args = parsed.args
    return await _get_messages_for_slash_command(
        command_name,
        args,
        context,
        preceding_input_blocks,
        image_content_blocks,
        attachment_messages,
        uuid,
    )


async def _get_messages_for_slash_command(
    command_name: str,
    args: str,
    context: ProcessUserInputContext,
    preceding_input_blocks: list[ContentBlockParam],
    image_content_blocks: list[ContentBlockParam],
    attachment_messages: list[dict[str, Any]],
    uuid: str | None = None,
) -> dict[str, Any]:
    """Return the messages for slash command."""
    from tabvis.ui.commands import get_command

    command = get_command(command_name, context.options.commands)

    if command.type == "prompt" and command.user_invocable is not False:
        record_skill_usage(command_name)

    if command.user_invocable is False:
        return {
            "messages": [
                create_user_message(
                    content=_prepare_user_content(
                        {
                            "inputString": f"/{command_name}",
                            "precedingInputBlocks": preceding_input_blocks,
                        }
                    ),
                ),
                create_user_message(
                    content=(
                        f'This skill can only be invoked by Tabvis, not directly by users. Ask Tabvis '
                        f'to use the "{command_name}" skill for you.'
                    ),
                ),
            ],
            "shouldQuery": False,
            "command": command,
        }

    try:
        if command.type == "local":
            return await _execute_local_command(
                command, args, context, preceding_input_blocks
            )
        if command.type == "prompt":
            return await _get_messages_for_prompt_slash_command(
                command,
                args,
                context,
                preceding_input_blocks,
                image_content_blocks,
                attachment_messages,
                uuid,
            )
        raise AssertionError(f"Unexpected command type: {command.type}")
    except Exception as error:
        if isinstance(error, _malformed_command_error()):
            return {
                "messages": [
                    create_user_message(
                        content=_prepare_user_content(
                            {
                                "inputString": str(error),
                                "precedingInputBlocks": preceding_input_blocks,
                            }
                        ),
                    ),
                ],
                "shouldQuery": False,
                "command": command,
            }
        raise


async def _execute_local_command(
    command: Command,
    args: str,
    context: ProcessUserInputContext,
    preceding_input_blocks: list[ContentBlockParam],
) -> dict[str, Any]:
    """Run a local command and wrap its result."""
    display_args = "***" if (command.is_sensitive and args.strip()) else args
    user_message = create_user_message(
        content=_prepare_user_content(
            {
                "inputString": _format_command_input(command, display_args),
                "precedingInputBlocks": preceding_input_blocks,
            }
        ),
    )

    try:
        synthetic_caveat_message = _create_synthetic_user_caveat_message()
        mod = await command.load()
        result = await mod["call"](args, context)

        if result["type"] == "skip":
            return {"messages": [], "shouldQuery": False, "command": command}

        if result["type"] == "compact":
            from tabvis.agent.compact.compact import build_post_compact_messages
            from tabvis.agent.compact.micro_compact import reset_microcompact_state

            display_text = result.get("displayText")
            slash_command_messages = [
                synthetic_caveat_message,
                user_message,
                *(
                    [
                        create_user_message(
                            content=(
                                f"<local-command-stdout>{display_text}</local-command-stdout>"
                            ),
                            timestamp=_iso_in_100ms(),
                        )
                    ]
                    if display_text
                    else []
                ),
            ]
            reset_microcompact_state()
            compaction_result = result["compactionResult"]
            return {
                "messages": build_post_compact_messages(
                    {
                        **compaction_result,
                        "messagesToKeep": [
                            *(compaction_result.get("messagesToKeep") or []),
                            *slash_command_messages,
                        ],
                    }
                ),
                "shouldQuery": False,
                "command": command,
            }

        return {
            "messages": [
                user_message,
                _create_command_input_message(
                    f"<local-command-stdout>{result.get('value') or NO_CONTENT_MESSAGE}"
                    f"</local-command-stdout>"
                ),
            ],
            "shouldQuery": False,
            "command": command,
            "resultText": result.get("value"),
        }
    except Exception as error:  # noqa: BLE001 - surfaced as local-command-stderr
        log_error(error)
        return {
            "messages": [
                user_message,
                _create_command_input_message(
                    f"<local-command-stderr>{str(error)}</local-command-stderr>"
                ),
            ],
            "shouldQuery": False,
            "command": command,
        }


def _format_command_input(command: Command, args: str) -> str:
    """Format the command input."""
    return _format_command_input_tags(get_command_name(command), args)


def format_skill_loading_metadata(skill_name: str, _progress_message: str = "loading") -> str:
    """Format the skill loading metadata."""
    return "\n".join(
        [
            f"<{COMMAND_MESSAGE_TAG}>{skill_name}</{COMMAND_MESSAGE_TAG}>",
            f"<{COMMAND_NAME_TAG}>{skill_name}</{COMMAND_NAME_TAG}>",
            "<skill-format>true</skill-format>",
        ]
    )


def _format_slash_command_loading_metadata(command_name: str, args: str | None = None) -> str:
    """Format the slash command loading metadata."""
    parts = [
        f"<{COMMAND_MESSAGE_TAG}>{command_name}</{COMMAND_MESSAGE_TAG}>",
        f"<{COMMAND_NAME_TAG}>/{command_name}</{COMMAND_NAME_TAG}>",
        f"<command-args>{args}</command-args>" if args else None,
    ]
    return "\n".join(p for p in parts if p)


def _format_command_loading_metadata(command: Command, args: str | None = None) -> str:
    """Format the command loading metadata."""
    if command.user_invocable is not False:
        return _format_slash_command_loading_metadata(command.name, args)
    if command.loaded_from in ("skills", "mcp"):
        return format_skill_loading_metadata(
            command.name, getattr(command, "progress_message", "loading")
        )
    return _format_slash_command_loading_metadata(command.name, args)


async def process_prompt_slash_command(
    command_name: str,
    args: str,
    commands: list[Command],
    context: ToolUseContext,
    image_content_blocks: list[ContentBlockParam] | None = None,
) -> dict[str, Any]:
    """Resolve + expand a prompt command directly."""
    from tabvis.ui.commands import find_command

    if image_content_blocks is None:
        image_content_blocks = []
    command = find_command(command_name, commands)
    if not command:
        raise _malformed_command_error()(f"Unknown command: {command_name}")
    if command.type != "prompt":
        raise RuntimeError(
            f"Unexpected {command.type} command. Expected 'prompt' command. Use "
            f"/{command_name} directly in the main conversation."
        )
    return await _get_messages_for_prompt_slash_command(
        command,
        args,
        context,
        [],
        image_content_blocks,
        [],
    )


async def _get_messages_for_prompt_slash_command(
    command: Command,
    args: str,
    context: ToolUseContext,
    preceding_input_blocks: list[ContentBlockParam] | None = None,
    image_content_blocks: list[ContentBlockParam] | None = None,
    existing_attachment_messages: list[dict[str, Any]] | None = None,
    uuid: str | None = None,
) -> dict[str, Any]:
    """Expand a skill / prompt command."""
    from tabvis.bootstrap.state import add_invoked_skill, get_session_id
    from tabvis.utils.attachments import get_attachment_messages
    from tabvis.utils.permissions.permission_setup import parse_tool_list_from_cli

    if preceding_input_blocks is None:
        preceding_input_blocks = []
    if image_content_blocks is None:
        image_content_blocks = []
    if existing_attachment_messages is None:
        existing_attachment_messages = []

    try:
        result = await command.get_prompt_for_command(args, context)

        if command.hooks:
            from tabvis.utils.hooks.register_skill_hooks import register_skill_hooks

            register_skill_hooks(
                context.set_app_state,
                get_session_id(),
                command.hooks,
                command.name,
                command.skill_root,
            )

        skill_path = (
            f"{command.source}:{command.name}"
            if getattr(command, "source", None)
            else command.name
        )
        skill_content = "\n\n".join(
            block["text"] for block in result if block.get("type") == "text"
        )
        agent_context = get_agent_context()
        add_invoked_skill(
            command.name,
            skill_path,
            skill_content,
            agent_context.agent_id if agent_context else None,
        )

        additional_allowed_tools = parse_tool_list_from_cli(command.allowed_tools or [])
        main_message_content: list[ContentBlockParam] = (
            [*image_content_blocks, *preceding_input_blocks, *result]
            if (len(image_content_blocks) > 0 or len(preceding_input_blocks) > 0)
            else result
        )
        attachment_messages = [
            *existing_attachment_messages,
            *(
                await to_array(
                    get_attachment_messages(
                        " ".join(
                            block["text"] for block in result if block.get("type") == "text"
                        ),
                        context,
                        None,
                        [],
                        context.messages,
                        "repl_main_thread",
                        {"skipSkillDiscovery": True},
                    )
                )
            ),
        ]

        return {
            "messages": [
                create_user_message(
                    content=_format_command_loading_metadata(command, args),
                    uuid=uuid,
                ),
                create_user_message(
                    content=main_message_content,
                    is_meta=True,
                ),
                *attachment_messages,
                _create_attachment_message(
                    {
                        "type": "command_permissions",
                        "allowedTools": additional_allowed_tools,
                        "model": command.model,
                    }
                ),
            ],
            "shouldQuery": True,
            "allowedTools": additional_allowed_tools,
            "model": command.model,
            "effort": command.effort,
            "command": command,
        }
    except Exception as error:
        from tabvis.utils.abort import AbortError

        if isinstance(error, AbortError):
            return {
                "messages": [
                    create_user_message(
                        content=_prepare_user_content(
                            {
                                "inputString": _format_command_input(command, args),
                                "precedingInputBlocks": preceding_input_blocks,
                            }
                        ),
                    ),
                    _create_user_interruption_message({"toolUse": False}),
                ],
                "shouldQuery": False,
                "command": command,
            }
        return {
            "messages": [
                create_user_message(
                    content=_prepare_user_content(
                        {
                            "inputString": _format_command_input(command, args),
                            "precedingInputBlocks": preceding_input_blocks,
                        }
                    ),
                ),
                create_user_message(
                    content=f"<local-command-stderr>{str(error)}</local-command-stderr>",
                ),
            ],
            "shouldQuery": False,
            "command": command,
        }


# ----------------------------------------------------------------------------------------------
# messages.ts helpers not yet on the existing surface — local fallbacks.
# ----------------------------------------------------------------------------------------------


def _iso_in_100ms() -> str:
    """``new Date(Date.now() + 100).toISOString()`` — a slightly-later ISO timestamp."""
    import time
    from datetime import datetime

    return (
        datetime.fromtimestamp(time.time() + 0.1, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _prepare_user_content(options: dict[str, Any]) -> Any:
    """Local stand-in for ``prepareUserContent`` (``messages.ts``)."""
    try:
        from tabvis.utils.messages import prepare_user_content  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - faithful fallback: prepend input string to the blocks
        input_string = options.get("inputString", "")
        blocks = options.get("precedingInputBlocks") or []
        if blocks:
            return [*blocks, {"type": "text", "text": input_string}]
        return input_string
    return prepare_user_content(options)


def _format_command_input_tags(command_name: str, args: str) -> str:
    """Local stand-in for ``formatCommandInputTags`` (``messages.ts``)."""
    try:
        from tabvis.utils.messages import format_command_input_tags  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - faithful fallback to the command-name/args tags
        name_tag = f"<{COMMAND_NAME_TAG}>/{command_name}</{COMMAND_NAME_TAG}>"
        return f"{name_tag}\n<command-args>{args}</command-args>" if args else name_tag
    return format_command_input_tags(command_name, args)


def _create_command_input_message(content: str) -> dict[str, Any]:
    """Local stand-in for ``createCommandInputMessage`` (``messages.ts``)."""
    try:
        from tabvis.utils.messages import create_command_input_message  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - faithful fallback: a meta user message
        return create_user_message(content=content, is_meta=True)
    return create_command_input_message(content)


def _create_synthetic_user_caveat_message() -> dict[str, Any]:
    """Local stand-in for ``createSyntheticUserCaveatMessage`` (``messages.ts``)."""
    try:
        from tabvis.utils.messages import (  # type: ignore[attr-defined]
            create_synthetic_user_caveat_message,
        )
    except Exception:  # noqa: BLE001 - faithful fallback: an empty meta user message
        return create_user_message(content="", is_meta=True)
    return create_synthetic_user_caveat_message()


def _create_user_interruption_message(options: dict[str, Any]) -> dict[str, Any]:
    """Local stand-in for ``createUserInterruptionMessage`` (``messages.ts``)."""
    try:
        from tabvis.utils.messages import (  # type: ignore[attr-defined]
            create_user_interruption_message,
        )
    except Exception:  # noqa: BLE001 - faithful fallback to the interrupt sentinel text
        from tabvis.utils.messages import INTERRUPT_MESSAGE

        return create_user_message(content=INTERRUPT_MESSAGE)
    return create_user_interruption_message(options)


def _create_attachment_message(attachment: dict[str, Any]) -> dict[str, Any]:
    """Wrap an attachment in an AttachmentMessage (``createAttachmentMessage``)."""
    from tabvis.utils.attachments import create_attachment_message

    return create_attachment_message(attachment)
