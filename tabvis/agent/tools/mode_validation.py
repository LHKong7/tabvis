"""Permission-mode validation for the Bash tool.

Decides whether a bash command is auto-handled by the current permission *mode*
(today: only ``acceptEdits`` auto-allows a small set of filesystem mutation
commands). Everything else falls through with ``behavior: 'passthrough'``.

Casing: Python identifiers are snake_case; the returned :data:`PermissionResult`
dicts keep their camelCase wire keys (``updatedInput``, ``decisionReason``)
because they round-trip into the permission/transcript layer.

Cycle note: this module imports ``BashToolInput`` from ``tabvis.agent.tools.bash_tool``
ONLY under ``TYPE_CHECKING`` (a top-level import would create a cycle once
``bash_tool`` is rewired to import the permission helpers). ``split_command_deprecated``
comes from the existing ``tabvis.utils.bash.commands``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tabvis.utils.bash.commands import split_command_deprecated

if TYPE_CHECKING:
    from tabvis.agent.tools.bash_tool import BashToolInput
    from tabvis.types.permissions import PermissionResult, ToolPermissionContext

# Filesystem mutation commands auto-allowed in Accept Edits mode.
# Commands allowed while the permission mode accepts edits.
ACCEPT_EDITS_ALLOWED_COMMANDS: tuple[str, ...] = (
    "mkdir",
    "touch",
    "rm",
    "rmdir",
    "mv",
    "cp",
    "sed",
)


def _is_filesystem_command(command: str) -> bool:
    """Base command is in the accept-edits allow-list."""
    return command in ACCEPT_EDITS_ALLOWED_COMMANDS


def _validate_command_for_mode(
    cmd: str,
    tool_permission_context: ToolPermissionContext,
) -> PermissionResult:
    """Single-subcommand mode check."""
    trimmed_cmd = cmd.strip()
    parts = trimmed_cmd.split()
    base_cmd = parts[0] if parts else ""

    if not base_cmd:
        return {
            "behavior": "passthrough",
            "message": "Base command not found",
        }

    # In Accept Edits mode, auto-allow filesystem operations.
    if (
        tool_permission_context.get("mode") == "acceptEdits"
        and _is_filesystem_command(base_cmd)
    ):
        return {
            "behavior": "allow",
            "updatedInput": {"command": cmd},
            "decisionReason": {
                "type": "mode",
                "mode": "acceptEdits",
            },
        }

    mode = tool_permission_context.get("mode")
    return {
        "behavior": "passthrough",
        "message": f"No mode-specific handling for '{base_cmd}' in {mode} mode",
    }


def check_permission_mode(
    input: BashToolInput | Any,
    tool_permission_context: ToolPermissionContext,
) -> PermissionResult:
    """Main entry point for mode-based permission logic.

    Returns:
      - ``'allow'`` if the current mode permits auto-approval
      - ``'ask'`` if the command needs approval in the current mode
      - ``'passthrough'`` if no mode-specific handling applies
    """
    mode = tool_permission_context.get("mode")

    # Skip if in bypass mode (handled elsewhere).
    if mode == "bypassPermissions":
        return {
            "behavior": "passthrough",
            "message": "Bypass mode is handled in main permission flow",
        }

    # Skip if in dontAsk mode (handled in main permission flow).
    if mode == "dontAsk":
        return {
            "behavior": "passthrough",
            "message": "DontAsk mode is handled in main permission flow",
        }

    commands = split_command_deprecated(_command_of(input))

    # Check each subcommand.
    for cmd in commands:
        result = _validate_command_for_mode(cmd, tool_permission_context)

        # If any command triggers mode-specific behavior, return that result.
        if result.get("behavior") != "passthrough":
            return result

    # No mode-specific handling needed.
    return {
        "behavior": "passthrough",
        "message": "No mode-specific validation required",
    }


def get_auto_allowed_commands(mode: str | None) -> tuple[str, ...]:
    """Accept-edits allow-list or empty."""
    return ACCEPT_EDITS_ALLOWED_COMMANDS if mode == "acceptEdits" else ()


def _command_of(input: BashToolInput | Any) -> str:
    """Read ``command`` from a pydantic ``BashToolInput`` or a plain dict."""
    if isinstance(input, dict):
        return input.get("command") or ""
    return getattr(input, "command", "") or ""
