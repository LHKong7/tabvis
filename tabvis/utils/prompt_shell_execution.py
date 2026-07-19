"""Parse and validate shell commands embedded in prompts.

Parses prompt text and executes any embedded shell commands. Supports two syntaxes:
- Code blocks:  ``` ! command ```
- Inline:       ``!`command` ``

NOTE: ``call()`` is invoked directly here, bypassing ``validate_input`` — any load-bearing check
must live in ``call()`` itself.

``shell`` is routed from .md frontmatter (author's choice) or is ``None`` for built-in commands;
it is *never* read from settings.defaultShell.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from tabvis.agent.tools.bash_tool import bash_tool
from tabvis.utils.crypto import random_uuid
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import get_error_message
from tabvis.utils.frontmatter_parser import FrontmatterShell
from tabvis.utils.messages import create_assistant_message
from tabvis.utils.tool_errors import ShellError
from tabvis.utils.tool_result_storage import process_tool_result_block

__all__ = ["MalformedCommandError", "execute_shell_commands_in_prompt"]


class MalformedCommandError(Exception):
    """Raised when a prompt contains an invalid shell command.

    Defined locally because ``tabvis.utils.errors`` does not (yet) expose it and existing modules may
    not be edited. Behaviourally identical: a plain ``Error`` subclass.
    """


# Pattern for code blocks: ```! command ```
_BLOCK_PATTERN = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")

# Pattern for inline: !`command` — requires whitespace or start-of-line before ``!``.
_INLINE_PATTERN = re.compile(r"(?:(?<=^)|(?<=\s))!`([^`]+)`", re.MULTILINE)


async def execute_shell_commands_in_prompt(
    text: str,
    context: Any,
    slash_command_name: str,
    shell: FrontmatterShell | None = None,
) -> str:
    """Parse prompt text and execute embedded shell commands, substituting their output."""
    result = text

    # All embedded shell commands run through BashTool.
    shell_tool = bash_tool

    block_matches = list(_BLOCK_PATTERN.finditer(text))
    # Gate the (slower) inline scan on a cheap substring check — 93% of skills have no ``!```.
    inline_matches = list(_INLINE_PATTERN.finditer(text)) if "!`" in text else []

    results = await asyncio.gather(
        *[
            _run_match(match, shell_tool, context, slash_command_name)
            for match in [*block_matches, *inline_matches]
        ],
        return_exceptions=True,
    )

    # Apply substitutions and surface the first raised error (mirrors Promise.all rejection).
    for match, outcome in zip([*block_matches, *inline_matches], results, strict=True):
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            full = match.group(0)
            # Replace ONLY the first occurrence of the matched pattern; use a function replacer so
            # ``$``-style sequences in shell output are never interpreted (re.sub w/ a callable).
            result = result.replace(full, outcome, 1)

    return result


async def _run_match(
    match: re.Match[str],
    shell_tool: Any,
    context: Any,
    slash_command_name: str,
) -> str | None:
    command = (match.group(1) or "").strip()
    if not command:
        return None
    try:
        # implemented — ``permissions.py`` exposes only the deny-rule helpers. Lazy-imported so this
        # module imports cleanly; the call is faithful to the TS once the matcher lands.
        from tabvis.utils.permissions.permissions import (  # type: ignore[attr-defined]
            has_permissions_to_use_tool,
        )

        permission_result = await has_permissions_to_use_tool(
            shell_tool,
            {"command": command},
            context,
            create_assistant_message(content=[]),
            "",
        )

        if permission_result.get("behavior") != "allow":
            log_for_debugging(
                f"Shell command permission check failed for command in {slash_command_name}: "
                f"{command}. Error: {permission_result.get('message')}"
            )
            raise MalformedCommandError(
                f'Shell command permission check failed for pattern "{match.group(0)}": '
                f"{permission_result.get('message') or 'Permission denied'}"
            )

        call_result = await shell_tool.call({"command": command}, context)
        data = call_result["data"]
        # Reuse the same persistence flow as regular Bash tool calls.
        tool_result_block = await process_tool_result_block(shell_tool, data, random_uuid())
        block_content = tool_result_block.get("content")
        output = (
            block_content
            if isinstance(block_content, str)
            else _format_bash_output(data["stdout"], data["stderr"])
        )
        return output
    except MalformedCommandError:
        raise
    except Exception as e:  # noqa: BLE001 — routed through the bash-error formatter (always raises)
        _format_bash_error(e, match.group(0))


def _format_bash_output(stdout: str, stderr: str, inline: bool = False) -> str:
    parts: list[str] = []
    if stdout.strip():
        parts.append(stdout.strip())
    if stderr.strip():
        if inline:
            parts.append(f"[stderr: {stderr.strip()}]")
        else:
            parts.append(f"[stderr]\n{stderr.strip()}")
    return (" " if inline else "\n").join(parts)


def _format_bash_error(e: Exception, pattern: str, inline: bool = False) -> None:
    """Always raises ``MalformedCommandError`` (TS return type ``never``)."""
    if isinstance(e, ShellError):
        if e.interrupted:
            raise MalformedCommandError(
                f'Shell command interrupted for pattern "{pattern}": [Command interrupted]'
            )
        output = _format_bash_output(e.stdout, e.stderr, inline)
        raise MalformedCommandError(
            f'Shell command failed for pattern "{pattern}": {output}'
        )

    message = get_error_message(e)
    formatted = f"[Error: {message}]" if inline else f"[Error]\n{message}"
    raise MalformedCommandError(formatted)
