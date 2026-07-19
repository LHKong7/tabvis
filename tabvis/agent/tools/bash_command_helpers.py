"""Command-operator permission helpers for the Bash tool.

Handles bash commands with operators that need behavior beyond simple
subcommand checking:

  * unsafe compound commands (subshells / command groups) → ``ask``;
  * piped commands → each pipe segment is checked independently through the full
    permission system (``segmented_command_permission_result``), with extra
    cross-segment guards for multiple ``cd`` and for ``cd`` + ``git`` (bare-repo
    fsmonitor bypass).

Casing: Python identifiers are snake_case; the returned :data:`PermissionResult`
dicts keep their camelCase wire keys (``updatedInput``, ``decisionReason``,
``suggestions``).

Cycle notes:
  * ``BASH_TOOL_NAME`` is inlined as the literal ``"Bash"`` (importing
    ``tabvis.agent.tools.bash_tool`` at module top would create a cycle once ``bash_tool``
    is rewired to import these helpers); ``BashToolInput`` is referenced only
    under ``TYPE_CHECKING``.
  * ``create_permission_request_message`` is not implemented in
    ``tabvis.utils.permissions.permissions``, so a local implementation covering the
    reason types this module emits (``other`` / ``subcommandResults`` / default)
    lives here.
  * ``bash_security`` / ``commands`` / ``parsed_command`` / ``parser`` are imported
    directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple

from tabvis.agent.tools.bash_security import bash_command_is_safe_async_deprecated
from tabvis.utils.bash.commands import (
    extract_output_redirections,
    is_unsafe_compound_command_deprecated,
    split_command_deprecated,
)
from tabvis.utils.bash.parsed_command import (
    ParsedCommand,
    build_parsed_command_from_root,
)
from tabvis.utils.bash.parser import PARSE_ABORTED
from tabvis.utils.string_utils import plural

if TYPE_CHECKING:
    from tabvis.agent.tools.bash_tool import BashToolInput  # noqa: F401
    from tabvis.types.permissions import PermissionResult
    from tabvis.utils.bash.parser import Node

# BASH_TOOL_NAME is "Bash" (matches tabvis/agent/tools/bash_tool.py). Inlined to avoid a
# top-level import of tabvis.agent.tools.bash_tool (cycle).
_BASH_TOOL_NAME = "Bash"


class CommandIdentityCheckers(NamedTuple):
    """A pair of predicates for identifying ``cd`` and ``git`` commands."""

    is_normalized_cd_command: Callable[[str], bool]
    is_normalized_git_command: Callable[[str], bool]


# ---------------------------------------------------------------------------
# Local permission-request message builder (only the branches used here).
# ---------------------------------------------------------------------------


def create_permission_request_message(
    tool_name: str,
    decision_reason: dict[str, Any] | None = None,
) -> str:
    """Build a human-readable permission-request message for a decision reason.

    Covers the reason types this module produces: ``other``/``safetyCheck`` (the ``reason``
    text as-is) and ``subcommandResults`` (lists the parts needing approval, stripping
    Bash output redirections for display). Falls back to the default message otherwise.
    """
    if decision_reason:
        reason_type = decision_reason.get("type")
        if reason_type in ("safetyCheck", "other", "workingDir", "asyncAgent"):
            return decision_reason.get("reason", "")
        if reason_type == "subcommandResults":
            needs_approval: list[str] = []
            for cmd, result in decision_reason.get("reasons", {}).items():
                behavior = result.get("behavior") if isinstance(result, dict) else None
                if behavior in ("ask", "passthrough"):
                    # Strip output redirections for display (Bash only) so filenames
                    # aren't shown as commands.
                    if tool_name == "Bash":
                        extracted = extract_output_redirections(cmd)
                        redirections = extracted.get("redirections", [])
                        display_cmd = (
                            extracted.get("commandWithoutRedirections", cmd)
                            if redirections
                            else cmd
                        )
                        needs_approval.append(display_cmd)
                    else:
                        needs_approval.append(cmd)
            if needs_approval:
                n = len(needs_approval)
                return (
                    f"This {tool_name} command contains multiple operations. "
                    f"The following {plural(n, 'part')} "
                    f"{plural(n, 'requires', 'require')} approval: "
                    f"{', '.join(needs_approval)}"
                )
            return (
                f"This {tool_name} command contains multiple operations "
                "that require approval"
            )

    return (
        f"Tabvis requested permissions to use {tool_name}, "
        "but you haven't granted it yet."
    )


# ---------------------------------------------------------------------------
# Segmented (piped) command handling.
# ---------------------------------------------------------------------------


async def segmented_command_permission_result(
    input: BashToolInput | dict[str, Any] | Any,
    segments: list[str],
    bash_tool_has_permission_fn: Callable[[Any], Any],
    checkers: CommandIdentityCheckers,
) -> PermissionResult:
    """Resolve the permission decision for a piped command's segments, one at a time."""
    # Check for multiple cd commands across all segments.
    cd_commands = [
        segment for segment in segments if checkers.is_normalized_cd_command(segment.strip())
    ]
    if len(cd_commands) > 1:
        decision_reason = {
            "type": "other",
            "reason": (
                "Multiple directory changes in one command require approval for clarity"
            ),
        }
        return {
            "behavior": "ask",
            "decisionReason": decision_reason,
            "message": create_permission_request_message(
                _BASH_TOOL_NAME, decision_reason
            ),
        }

    # SECURITY: cd+git across pipe segments → bare-repo fsmonitor bypass guard.
    has_cd = False
    has_git = False
    for segment in segments:
        subcommands = split_command_deprecated(segment)
        for sub in subcommands:
            trimmed = sub.strip()
            if checkers.is_normalized_cd_command(trimmed):
                has_cd = True
            if checkers.is_normalized_git_command(trimmed):
                has_git = True
    if has_cd and has_git:
        decision_reason = {
            "type": "other",
            "reason": (
                "Compound commands with cd and git require approval "
                "to prevent bare repository attacks"
            ),
        }
        return {
            "behavior": "ask",
            "decisionReason": decision_reason,
            "message": create_permission_request_message(
                _BASH_TOOL_NAME, decision_reason
            ),
        }

    # Check each non-empty segment through the full permission system.
    # Insertion-ordered dict preserves segment encounter order.
    segment_results: dict[str, Any] = {}
    for segment in segments:
        trimmed_segment = segment.strip()
        if not trimmed_segment:
            continue  # Skip empty segments.
        segment_result = await bash_tool_has_permission_fn(
            {**_as_dict(input), "command": trimmed_segment}
        )
        segment_results[trimmed_segment] = segment_result

    # Check if any segment is denied (after evaluating all).
    denied_segment = next(
        (
            (cmd, result)
            for cmd, result in segment_results.items()
            if result.get("behavior") == "deny"
        ),
        None,
    )

    if denied_segment is not None:
        segment_command, segment_result = denied_segment
        return {
            "behavior": "deny",
            "message": (
                segment_result.get("message")
                if segment_result.get("behavior") == "deny"
                else f"Permission denied for: {segment_command}"
            ),
            "decisionReason": {
                "type": "subcommandResults",
                "reasons": segment_results,
            },
        }

    all_allowed = all(
        result.get("behavior") == "allow" for result in segment_results.values()
    )

    if all_allowed:
        return {
            "behavior": "allow",
            "updatedInput": _as_dict(input),
            "decisionReason": {
                "type": "subcommandResults",
                "reasons": segment_results,
            },
        }

    decision_reason = {
        "type": "subcommandResults",
        "reasons": segment_results,
    }

    # No segment was denied (handled above) and not all are "allow" — so the rest are "passthrough"
    # and/or "ask". Only hard-ask if a segment actually ASKED; otherwise PASS THROUGH so the command
    # falls to the normal resolver instead of being denied in headless mode (where ask -> deny). This
    # restores parity with the &&/;/, subcommand path (which returns passthrough when no subcommand
    # asks), so benign pipes like `forge test | tail` or `forge build 2>&1 | grep` are allowed.
    any_ask = any(result.get("behavior") == "ask" for result in segment_results.values())
    if not any_ask:
        return {
            "behavior": "passthrough",
            "decisionReason": decision_reason,
        }

    # Collect suggestions from segments that need approval.
    suggestions: list[Any] = []
    for result in segment_results.values():
        if (
            result.get("behavior") != "allow"
            and "suggestions" in result
            and result.get("suggestions")
        ):
            suggestions.extend(result["suggestions"])

    return {
        "behavior": "ask",
        "message": create_permission_request_message(_BASH_TOOL_NAME, decision_reason),
        "decisionReason": decision_reason,
        "suggestions": suggestions if len(suggestions) > 0 else None,
    }


async def _build_segment_without_redirections(segment_command: str) -> str:
    """Strip output redirections from a pipe segment while preserving quoting
    (so filenames aren't treated as commands)."""
    # Fast path: skip parsing if no redirection operators present.
    if ">" not in segment_command:
        return segment_command

    parsed = await ParsedCommand.parse(segment_command)
    if parsed is None:
        return segment_command
    return parsed.without_output_redirections()


async def check_command_operator_permissions(
    input: BashToolInput | dict[str, Any] | Any,
    bash_tool_has_permission_fn: Callable[[Any], Any],
    checkers: CommandIdentityCheckers,
    ast_root: Node | None | Any,
) -> PermissionResult:
    """Resolve a parsed command (from a pre-parsed AST root if available, else via
    ``ParsedCommand.parse``) and delegate to
    :func:`_bash_tool_check_command_operator_permissions`."""
    command = _command_of(input)
    if ast_root is not None and ast_root is not PARSE_ABORTED:
        parsed = build_parsed_command_from_root(command, ast_root)
    else:
        parsed = await ParsedCommand.parse(command)
    if not parsed:
        return {"behavior": "passthrough", "message": "Failed to parse command"}
    return await _bash_tool_check_command_operator_permissions(
        input, bash_tool_has_permission_fn, checkers, parsed
    )


async def _bash_tool_check_command_operator_permissions(
    input: BashToolInput | dict[str, Any] | Any,
    bash_tool_has_permission_fn: Callable[[Any], Any],
    checkers: CommandIdentityCheckers,
    parsed: Any,
) -> PermissionResult:
    """Check permissions for a command's shell operators (subshells, command groups, pipes)."""
    command = _command_of(input)

    # 1. Check for unsafe compound commands (subshells, command groups).
    ts_analysis = parsed.get_tree_sitter_analysis()
    if ts_analysis is not None:
        compound = _get(ts_analysis, "compoundStructure") or {}
        is_unsafe_compound = bool(
            _get(compound, "hasSubshell") or _get(compound, "hasCommandGroup")
        )
    else:
        is_unsafe_compound = is_unsafe_compound_command_deprecated(command)
    if is_unsafe_compound:
        # Contains an operator like `>` we don't support as a subcommand separator.
        # Check if the legacy safety check has a more specific message.
        safety_result = await bash_command_is_safe_async_deprecated(command)
        reason = (
            safety_result.get("message")
            if (
                safety_result.get("behavior") == "ask"
                and safety_result.get("message")
            )
            else "This command uses shell operators that require approval for safety"
        )
        decision_reason = {"type": "other", "reason": reason}
        return {
            "behavior": "ask",
            "message": create_permission_request_message(
                _BASH_TOOL_NAME, decision_reason
            ),
            "decisionReason": decision_reason,
        }

    # 2. Check for piped commands using ParsedCommand (preserves quotes).
    pipe_segments = parsed.get_pipe_segments()

    # If no pipes (single segment), let normal flow handle it.
    if len(pipe_segments) <= 1:
        return {"behavior": "passthrough", "message": "No pipes found in command"}

    # Strip output redirections from each segment while preserving quotes.
    segments = [
        await _build_segment_without_redirections(segment) for segment in pipe_segments
    ]

    # Handle as segmented command.
    return await segmented_command_permission_result(
        input, segments, bash_tool_has_permission_fn, checkers
    )


# ---------------------------------------------------------------------------
# Small accessors (the input may be a pydantic model or a plain dict).
# ---------------------------------------------------------------------------


def _command_of(input: BashToolInput | dict[str, Any] | Any) -> str:
    if isinstance(input, dict):
        return input.get("command") or ""
    return getattr(input, "command", "") or ""


def _as_dict(input: BashToolInput | dict[str, Any] | Any) -> dict[str, Any]:
    """Shallow-copy the input into a plain dict (spread-equivalent of ``{...input}``)."""
    if isinstance(input, dict):
        return dict(input)
    if hasattr(input, "model_dump"):
        return input.model_dump()
    return dict(getattr(input, "__dict__", {}))


def _get(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
