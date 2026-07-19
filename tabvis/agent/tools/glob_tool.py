"""Glob file-search tool — fast file-pattern matching.

Fast file-pattern matching (``**/*.js`` etc.) returning matching paths sorted by
modification time. Read-only and concurrency-safe. The heavy lifting lives in
:func:`tabvis.utils.glob.glob` (rg / pure-Python walker); this tool wraps it with input
validation, path expansion, relativization, and the tool-result rendering.

Casing: Python identifiers are snake_case; the ``Output`` dict and the tool_result block
keep their wire keys (``filenames``/``durationMs``/``numFiles``/``truncated``;
``tool_use_id``/``type``/``content``).
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext, ValidationResult
from tabvis.utils.cwd import get_cwd
from tabvis.utils.errors import is_enoent
from tabvis.utils.file import FILE_NOT_FOUND_CWD_NOTE, suggest_path_under_cwd
from tabvis.utils.glob import glob
from tabvis.utils.path import expand_path, to_relative_path

# ---------------------------------------------------------------------------
# prompt.ts constants
# ---------------------------------------------------------------------------

GLOB_TOOL_NAME = "Glob"

DESCRIPTION = """- Fast file pattern matching tool that works with any codebase size
- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns
- When you are doing an open ended search that may require multiple rounds of globbing and grepping, use the Agent tool instead"""


# ---------------------------------------------------------------------------
# input schema
# ---------------------------------------------------------------------------


class GlobInput(BaseModel):
    """Validated input for :class:`GlobTool`."""

    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(description="The glob pattern to match files against")
    path: str | None = Field(
        default=None,
        description=(
            "The directory to search in. If not specified, the current working directory "
            'will be used. IMPORTANT: Omit this field to use the default directory. DO NOT '
            'enter "undefined" or "null" - simply omit it for the default behavior. Must be a '
            "valid directory path if provided."
        ),
    )


# ---------------------------------------------------------------------------
# Permission matcher stub.
#
# The permissions package (tabvis/utils/permissions/*) is not implemented yet, and this
# build has no configured permission rules, so this matcher is never consulted in
# practice. Replace with a real wildcard-pattern matcher when the permissions layer
# lands.
# ---------------------------------------------------------------------------


def _match_wildcard_pattern(rule_pattern: str, pattern: str) -> bool:  # noqa: ARG001
    return False


def _get_tool_use_summary(input: dict[str, Any] | None) -> str | None:
    if not input:
        return None
    return input.get("pattern")


class GlobTool(Tool):
    name = GLOB_TOOL_NAME
    search_hint = "find files by name pattern or wildcard"
    input_schema = GlobInput
    max_result_size_chars = 100_000

    # GlobTool is a pure search — safe to run concurrently and never mutates state.
    def is_concurrency_safe(self, input: Any) -> bool:
        return True

    def is_read_only(self, input: Any) -> bool:
        return True

    def is_search_or_read_command(self, input: Any) -> dict[str, bool] | None:
        return {"isSearch": True, "isRead": False}

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:
        return DESCRIPTION

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return _get_tool_use_summary(input)

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = _get_tool_use_summary(input)
        return f"Finding {summary}" if summary else "Finding files"

    def get_path(self, input: Any) -> str | None:
        path = input.get("path") if isinstance(input, dict) else getattr(input, "path", None)
        return expand_path(path) if path else get_cwd()

    async def prepare_permission_matcher(self, input: Any):
        pattern = (
            input.get("pattern")
            if isinstance(input, dict)
            else getattr(input, "pattern", "")
        )

        def matcher(rule_pattern: str) -> bool:
            return _match_wildcard_pattern(rule_pattern, pattern)

        return matcher

    async def validate_input(
        self, input: Any, context: ToolUseContext
    ) -> ValidationResult:
        path = input.get("path") if isinstance(input, dict) else getattr(input, "path", None)
        # If path is provided, validate that it exists and is a directory.
        if path:
            import os

            absolute_path = expand_path(path)

            # SECURITY: skip filesystem ops for UNC paths to prevent NTLM credential leaks.
            if absolute_path.startswith("\\\\") or absolute_path.startswith("//"):
                return ValidationResult(result=True)

            try:
                stats = os.stat(absolute_path)
            except OSError as e:
                if is_enoent(e):
                    cwd_suggestion = await suggest_path_under_cwd(absolute_path)
                    message = (
                        f"Directory does not exist: {path}. "
                        f"{FILE_NOT_FOUND_CWD_NOTE} {get_cwd()}."
                    )
                    if cwd_suggestion:
                        message += f" Did you mean {cwd_suggestion}?"
                    return ValidationResult(result=False, message=message, error_code=1)
                raise

            if not _stat_is_dir(stats):
                return ValidationResult(
                    result=False,
                    message=f"Path is not a directory: {path}",
                    error_code=2,
                )

        return ValidationResult(result=True)

    async def check_permissions(self, input: Any, context: ToolUseContext):
        # The permissions package is not implemented and this build has no configured
        # rules, so allow.
        return {"behavior": "allow", "updatedInput": input}

    def extract_search_text(self, out: Any) -> str | None:
        # Reuses Grep's render — shows the filename list joined by newlines.
        filenames = out.get("filenames") if isinstance(out, dict) else getattr(out, "filenames", [])
        return "\n".join(filenames or [])

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool,
        parent_message,
        on_progress=None,
    ) -> ToolResult[dict[str, Any]]:
        start = time.monotonic()

        # None-safe access — this build may have no app state.
        app_state = context.get_app_state() if context.get_app_state else None
        tool_permission_context = (
            getattr(app_state, "toolPermissionContext", None)
            if app_state is not None
            else None
        )
        if tool_permission_context is None and isinstance(app_state, dict):
            tool_permission_context = app_state.get("toolPermissionContext")

        glob_limits = context.glob_limits or {}
        limit = glob_limits.get("maxResults", 100)

        result = await glob(
            args.pattern,
            self.get_path(args),
            {"limit": limit, "offset": 0},
            context.abort_controller.signal,
            tool_permission_context,
        )
        files = result["files"]
        truncated = result["truncated"]

        # Relativize paths under cwd to save tokens (same as GrepTool).
        filenames = [to_relative_path(p) for p in files]

        output: dict[str, Any] = {
            "filenames": filenames,
            "durationMs": int((time.monotonic() - start) * 1000),
            "numFiles": len(filenames),
            "truncated": truncated,
        }
        return ToolResult(data=output)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        filenames = content["filenames"]
        if len(filenames) == 0:
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": "No files found",
            }
        lines = [*filenames]
        if content["truncated"]:
            lines.append(
                "(Results are truncated. Consider using a more specific path or pattern.)"
            )
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": "\n".join(lines),
        }


def _stat_is_dir(stats: Any) -> bool:
    import stat as _stat

    try:
        return _stat.S_ISDIR(stats.st_mode)
    except AttributeError:
        return False


# Singleton instance used throughout the tool registry.
glob_tool = GlobTool()
