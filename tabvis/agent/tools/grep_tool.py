"""Grep tool — content search over files via ripgrep.

A search tool exposed to the model as :data:`grep_tool`. It subclasses
:class:`tabvis.tool.Tool`, with a pydantic v2 ``BaseModel`` (:class:`GrepToolInput`,
``extra='forbid'``) validating its input.

Notes on the wire format:

* The hyphenated ripgrep flags ``-A``/``-B``/``-C``/``-n``/``-i`` are real wire property names in
  the API schema (the model passes ``{"-A": 3}``). They map to ``Field(alias="-A", ...)`` with
  ``populate_by_name=True`` so BOTH ``model_json_schema(by_alias=True)`` emits the hyphen property
  AND validation accepts the hyphen key. Python attribute access uses the snake_case field names
  (``context_after`` etc.) — but the JSON Schema / validated input keep the wire form.
* ``semanticNumber``/``semanticBoolean`` coercion is implemented as ``mode="before"`` field
  validators: a numeric string literal (``/^-?\\d+(\\.\\d+)?$/``) is coerced to a number,
  ``"true"``/``"false"`` to a bool, and anything else passes through to be rejected by the
  field's declared type. The advertised schema type stays ``number``/``boolean`` — the string
  tolerance is invisible client-side coercion.
* ``output_mode`` (``content`` / ``files_with_matches`` / ``count``) selects the ripgrep flags
  and the corresponding result-formatting branch in
  ``map_tool_result_to_tool_result_block_param``.

The actual search is delegated to :func:`tabvis.utils.ripgrep.rip_grep` (system ``rg`` when
present, else a pure-Python walker). Path expansion/relativization come from
:mod:`tabvis.utils.path`.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tabvis.tool import Tool, ToolResult, ToolUseContext, ValidationResult
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.types.message import AssistantMessage
from tabvis.types.permissions import PermissionDecision
from tabvis.utils.cwd import get_cwd
from tabvis.utils.errors import is_enoent
from tabvis.utils.file import FILE_NOT_FOUND_CWD_NOTE, suggest_path_under_cwd
from tabvis.utils.path import expand_path, to_relative_path
from tabvis.utils.ripgrep import rip_grep

GREP_TOOL_NAME = "Grep"

# Names of tools referenced in the description text below, inlined to avoid importing
# their modules just for these constants.
AGENT_TOOL_NAME = "Task"
BASH_TOOL_NAME = "Bash"


def get_description() -> str:
    """Build the usage description text shown to the model for the Grep tool."""
    return (
        "A powerful search tool built on ripgrep\n"
        "\n"
        "  Usage:\n"
        f"  - ALWAYS use {GREP_TOOL_NAME} for search tasks. NEVER invoke `grep` or `rg` as a "
        f"{BASH_TOOL_NAME} command. The {GREP_TOOL_NAME} tool has been optimized for correct "
        "permissions and access.\n"
        '  - Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")\n'
        '  - Filter files with glob parameter (e.g., "*.js", "**/*.tsx") or type parameter '
        '(e.g., "js", "py", "rust")\n'
        '  - Output modes: "content" shows matching lines, "files_with_matches" shows only file '
        'paths (default), "count" shows match counts\n'
        f"  - Use {AGENT_TOOL_NAME} tool for open-ended searches requiring multiple rounds\n"
        "  - Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping (use "
        "`interface\\{\\}` to find `interface{}` in Go code)\n"
        "  - Multiline matching: By default patterns match within single lines only. For "
        "cross-line patterns like `struct \\{[\\s\\S]*?field`, use `multiline: true`\n"
    )


# ----------------------------------------------------------------------------------------------
# semanticNumber / semanticBoolean coercion
# ----------------------------------------------------------------------------------------------

_NUMERIC_LITERAL = re.compile(r"^-?\d+(\.\d+)?$")


def _semantic_number(value: Any) -> Any:
    """Coerce a numeric string literal to a number; pass anything else through.

    Only strings matching ``/^-?\\d+(\\.\\d+)?$/`` and parsing to a finite number are
    coerced (to ``int`` when integral, else ``float``). Empty strings / ``None`` / other
    shapes pass through unchanged so the field's declared type rejects them.
    """
    if isinstance(value, str) and _NUMERIC_LITERAL.match(value):
        try:
            num = float(value)
        except ValueError:
            return value
        if num != num or num in (float("inf"), float("-inf")):  # NaN/inf guard (Number.isFinite)
            return value
        return int(num) if num.is_integer() else num
    return value


def _semantic_boolean(value: Any) -> Any:
    """Coerce ``"true"``/``"false"`` to bool; pass anything else through.

    Coercion is by exact string match, not by truthiness — the string ``"false"`` becomes
    ``False`` (a merely non-empty string would otherwise be truthy).
    """
    if value == "true":
        return True
    if value == "false":
        return False
    return value


# ----------------------------------------------------------------------------------------------
# Input schema
# ----------------------------------------------------------------------------------------------

OutputMode = Literal["content", "files_with_matches", "count"]


class GrepToolInput(BaseModel):
    """Validated input for :data:`grep_tool`.

    ``populate_by_name=True`` lets validation accept either the hyphenated alias (``-A``) — the
    wire form the model emits — or the snake_case attribute name. ``by_alias=True`` in
    :meth:`model_json_schema` (set on the class) makes the advertised JSON Schema use the hyphens.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pattern: str = Field(
        description="The regular expression pattern to search for in file contents",
    )
    path: str | None = Field(
        default=None,
        description=(
            "File or directory to search in (rg PATH). Defaults to current working directory."
        ),
    )
    glob: str | None = Field(
        default=None,
        description=(
            'Glob pattern to filter files (e.g. "*.js", "*.{ts,tsx}") - maps to rg --glob'
        ),
    )
    output_mode: OutputMode | None = Field(
        default=None,
        description=(
            'Output mode: "content" shows matching lines (supports -A/-B/-C context, -n line '
            'numbers, head_limit), "files_with_matches" shows file paths (supports head_limit), '
            '"count" shows match counts (supports head_limit). Defaults to "files_with_matches".'
        ),
    )
    context_before: float | None = Field(
        default=None,
        alias="-B",
        description=(
            "Number of lines to show before each match (rg -B). Requires output_mode: "
            '"content", ignored otherwise.'
        ),
    )
    context_after: float | None = Field(
        default=None,
        alias="-A",
        description=(
            "Number of lines to show after each match (rg -A). Requires output_mode: "
            '"content", ignored otherwise.'
        ),
    )
    context_c: float | None = Field(
        default=None,
        alias="-C",
        description="Alias for context.",
    )
    context: float | None = Field(
        default=None,
        description=(
            "Number of lines to show before and after each match (rg -C). Requires "
            'output_mode: "content", ignored otherwise.'
        ),
    )
    show_line_numbers: bool | None = Field(
        default=None,
        alias="-n",
        description=(
            "Show line numbers in output (rg -n). Requires output_mode: "
            '"content", ignored otherwise. Defaults to true.'
        ),
    )
    case_insensitive: bool | None = Field(
        default=None,
        alias="-i",
        description="Case insensitive search (rg -i)",
    )
    type: str | None = Field(
        default=None,
        description=(
            "File type to search (rg --type). Common types: js, py, rust, go, java, etc. "
            "More efficient than include for standard file types."
        ),
    )
    head_limit: float | None = Field(
        default=None,
        description=(
            'Limit output to first N lines/entries, equivalent to "| head -N". Works across all '
            "output modes: content (limits output lines), files_with_matches (limits file paths), "
            "count (limits count entries). Defaults to 250 when unspecified. Pass 0 for unlimited "
            "(use sparingly — large result sets waste context)."
        ),
    )
    offset: float | None = Field(
        default=None,
        description=(
            "Skip first N lines/entries before applying head_limit, equivalent to "
            '"| tail -n +N | head -N". Works across all output modes. Defaults to 0.'
        ),
    )
    multiline: bool | None = Field(
        default=None,
        description=(
            "Enable multiline mode where . matches newlines and patterns can span lines "
            "(rg -U --multiline-dotall). Default: false."
        ),
    )

    @field_validator(
        "context_before",
        "context_after",
        "context_c",
        "context",
        "head_limit",
        "offset",
        mode="before",
    )
    @classmethod
    def _coerce_semantic_number(cls, value: Any) -> Any:
        return _semantic_number(value)

    @field_validator(
        "show_line_numbers",
        "case_insensitive",
        "multiline",
        mode="before",
    )
    @classmethod
    def _coerce_semantic_boolean(cls, value: Any) -> Any:
        return _semantic_boolean(value)


# ----------------------------------------------------------------------------------------------
# Constants + helpers
# ----------------------------------------------------------------------------------------------

# Version control system directories to exclude from searches (noise from VCS metadata).
VCS_DIRECTORIES_TO_EXCLUDE = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")

# Default cap on grep results when head_limit is unspecified. Pass head_limit=0
# explicitly for unlimited.
DEFAULT_HEAD_LIMIT = 250


def _plural(n: int, word: str, plural_word: str | None = None) -> str:
    """Return the singular or plural form of ``word`` based on ``n``."""
    return word if n == 1 else (plural_word if plural_word is not None else word + "s")


def apply_head_limit(
    items: list[Any], limit: int | None, offset: int = 0
) -> tuple[list[Any], int | None]:
    """Slice ``items[offset : offset+limit]``.

    Returns ``(sliced_items, applied_limit)`` where ``applied_limit`` is only set when truncation
    actually occurred (so the caller knows there may be more results to paginate). ``limit == 0``
    is the explicit "unlimited" escape hatch.
    """
    if limit == 0:
        return items[offset:], None
    effective_limit = limit if limit is not None else DEFAULT_HEAD_LIMIT
    sliced = items[offset : offset + effective_limit]
    was_truncated = (len(items) - offset) > effective_limit
    return sliced, (effective_limit if was_truncated else None)


def format_limit_info(applied_limit: int | None, applied_offset: int | None) -> str:
    """Build ``"limit: N, offset: M"`` (parts only when present)."""
    parts: list[str] = []
    if applied_limit is not None:
        parts.append(f"limit: {applied_limit}")
    if applied_offset:
        parts.append(f"offset: {applied_offset}")
    return ", ".join(parts)


def _get_tool_use_summary(input_obj: Any) -> str | None:
    """Return the search pattern, or ``None``."""
    if input_obj is None:
        return None
    if isinstance(input_obj, GrepToolInput):
        return input_obj.pattern or None
    if isinstance(input_obj, dict):
        return input_obj.get("pattern") or None
    return getattr(input_obj, "pattern", None) or None


def _num_to_arg(value: float) -> str:
    """Stringify a context/limit number without a trailing ``.0`` for whole values."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# ----------------------------------------------------------------------------------------------
# Tool
# ----------------------------------------------------------------------------------------------


class GrepTool(Tool):
    """``Grep`` — search file contents with regex (ripgrep)."""

    name = GREP_TOOL_NAME
    search_hint = "search file contents with regex (ripgrep)"
    input_schema = GrepToolInput
    # 20K chars — tool result persistence threshold.
    max_result_size_chars = 20_000
    strict = True

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return get_description()

    async def prompt(self, options: dict[str, Any]) -> str:
        return get_description()

    def user_facing_name(self, input: Any | None = None) -> str:
        return "Search"

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return _get_tool_use_summary(input)

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = _get_tool_use_summary(input)
        return f"Searching for {summary}" if summary else "Searching"

    def is_concurrency_safe(self, input: Any) -> bool:
        return True

    def is_read_only(self, input: Any) -> bool:
        return True

    def is_search_or_read_command(self, input: Any) -> dict[str, bool] | None:
        return {"isSearch": True, "isRead": False}

    def get_path(self, input: Any) -> str | None:
        path = getattr(input, "path", None) if not isinstance(input, dict) else input.get("path")
        return path or get_cwd()

    async def prepare_permission_matcher(self, input: Any):
        # This build has no configured rules to match against. Fall back to never-match.
        return lambda _rule_pattern: False

    def extract_search_text(self, out: Any) -> str | None:
        mode = out.get("mode") if isinstance(out, dict) else None
        content = out.get("content") if isinstance(out, dict) else None
        filenames = (out.get("filenames") if isinstance(out, dict) else None) or []
        if mode == "content" and content:
            return content
        return "\n".join(filenames)

    async def validate_input(self, input: Any, context: ToolUseContext) -> ValidationResult:
        path = getattr(input, "path", None)
        if not path:
            return ValidationResult(result=True)

        absolute_path = expand_path(path)

        # SECURITY: skip filesystem operations for UNC paths to prevent NTLM credential leaks.
        if absolute_path.startswith("\\\\") or absolute_path.startswith("//"):
            return ValidationResult(result=True)

        try:
            await asyncio.to_thread(os.stat, absolute_path)
        except OSError as e:
            if is_enoent(e):
                cwd_suggestion = await suggest_path_under_cwd(absolute_path)
                message = (
                    f"Path does not exist: {path}. {FILE_NOT_FOUND_CWD_NOTE} {get_cwd()}."
                )
                if cwd_suggestion:
                    message += f" Did you mean {cwd_suggestion}?"
                return ValidationResult(result=False, message=message, error_code=1)
            raise

        return ValidationResult(result=True)

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        # Allows reads when no deny rule matches (no deny rules are configured in this build).
        return {"behavior": "allow", "updatedInput": input}

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        mode = data.get("mode", "files_with_matches")
        num_files = data.get("numFiles", 0)
        filenames = data.get("filenames", []) or []
        out_content = data.get("content")
        num_matches = data.get("numMatches")
        applied_limit = data.get("appliedLimit")
        applied_offset = data.get("appliedOffset")

        if mode == "content":
            limit_info = format_limit_info(applied_limit, applied_offset)
            result_content = out_content or "No matches found"
            final_content = (
                f"{result_content}\n\n[Showing results with pagination = {limit_info}]"
                if limit_info
                else result_content
            )
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": final_content,
            }

        if mode == "count":
            limit_info = format_limit_info(applied_limit, applied_offset)
            raw_content = out_content or "No matches found"
            matches = num_matches if num_matches is not None else 0
            files = num_files if num_files is not None else 0
            occurrence = "occurrence" if matches == 1 else "occurrences"
            file_word = "file" if files == 1 else "files"
            pagination = f" with pagination = {limit_info}" if limit_info else ""
            summary = (
                f"\n\nFound {matches} total {occurrence} across {files} {file_word}.{pagination}"
            )
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": raw_content + summary,
            }

        # files_with_matches mode
        limit_info = format_limit_info(applied_limit, applied_offset)
        if num_files == 0:
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": "No files found",
            }
        suffix = f" {limit_info}" if limit_info else ""
        result = (
            f"Found {num_files} {_plural(num_files, 'file')}{suffix}\n" + "\n".join(filenames)
        )
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": result,
        }

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[Any]:
        pattern: str = args.pattern
        path: str | None = args.path
        glob: str | None = args.glob
        type_filter: str | None = args.type
        output_mode: str = args.output_mode or "files_with_matches"
        context_before = args.context_before
        context_after = args.context_after
        context_c = args.context_c
        context_val = args.context
        show_line_numbers = True if args.show_line_numbers is None else args.show_line_numbers
        case_insensitive = False if args.case_insensitive is None else args.case_insensitive
        head_limit = args.head_limit
        offset = int(args.offset) if args.offset is not None else 0
        multiline = False if args.multiline is None else args.multiline

        absolute_path = expand_path(path) if path else get_cwd()
        rg_args: list[str] = ["--hidden"]

        # Exclude VCS directories to avoid noise from version control metadata.
        for vcs_dir in VCS_DIRECTORIES_TO_EXCLUDE:
            rg_args.extend(["--glob", f"!{vcs_dir}"])

        # Limit line length to prevent base64/minified content from cluttering output.
        rg_args.extend(["--max-columns", "500"])

        if multiline:
            rg_args.extend(["-U", "--multiline-dotall"])

        if case_insensitive:
            rg_args.append("-i")

        if output_mode == "files_with_matches":
            rg_args.append("-l")
        elif output_mode == "count":
            rg_args.append("-c")

        if show_line_numbers and output_mode == "content":
            rg_args.append("-n")

        # Context flags (-C/context takes precedence over -B/-A).
        if output_mode == "content":
            if context_val is not None:
                rg_args.extend(["-C", _num_to_arg(context_val)])
            elif context_c is not None:
                rg_args.extend(["-C", _num_to_arg(context_c)])
            else:
                if context_before is not None:
                    rg_args.extend(["-B", _num_to_arg(context_before)])
                if context_after is not None:
                    rg_args.extend(["-A", _num_to_arg(context_after)])

        # If pattern starts with dash, use -e so rg doesn't treat it as a flag.
        if pattern.startswith("-"):
            rg_args.extend(["-e", pattern])
        else:
            rg_args.append(pattern)

        if type_filter:
            rg_args.extend(["--type", type_filter])

        if glob:
            # Split on commas and spaces, but preserve patterns with braces.
            glob_patterns: list[str] = []
            raw_patterns = re.split(r"\s+", glob)
            for raw_pattern in raw_patterns:
                if "{" in raw_pattern and "}" in raw_pattern:
                    glob_patterns.append(raw_pattern)
                else:
                    glob_patterns.extend(p for p in raw_pattern.split(",") if p)
            for glob_pattern in (p for p in glob_patterns if p):
                rg_args.extend(["--glob", glob_pattern])

        # Ignore-pattern normalization is not implemented; this build has no configured
        # ignore patterns, so no extra negated globs are appended.

        results = await rip_grep(rg_args, absolute_path)

        head_limit_int = int(head_limit) if head_limit is not None else None

        if output_mode == "content":
            # Apply head_limit first (relativize is per-line work; broad patterns return 10k+
            # lines with head_limit keeping only ~30-100).
            limited_results, applied_limit = apply_head_limit(results, head_limit_int, offset)
            final_lines: list[str] = []
            for line in limited_results:
                # Lines: /absolute/path:content or /absolute/path:num:content
                colon_index = line.find(":")
                if colon_index > 0:
                    file_path = line[:colon_index]
                    rest = line[colon_index:]
                    final_lines.append(to_relative_path(file_path) + rest)
                else:
                    final_lines.append(line)
            output: dict[str, Any] = {
                "mode": "content",
                "numFiles": 0,
                "filenames": [],
                "content": "\n".join(final_lines),
                "numLines": len(final_lines),
            }
            if applied_limit is not None:
                output["appliedLimit"] = applied_limit
            if offset > 0:
                output["appliedOffset"] = offset
            return ToolResult(data=output)

        if output_mode == "count":
            limited_results, applied_limit = apply_head_limit(results, head_limit_int, offset)
            final_count_lines: list[str] = []
            for line in limited_results:
                # Lines: /absolute/path:count
                colon_index = line.rfind(":")
                if colon_index > 0:
                    file_path = line[:colon_index]
                    count_part = line[colon_index:]
                    final_count_lines.append(to_relative_path(file_path) + count_part)
                else:
                    final_count_lines.append(line)

            total_matches = 0
            file_count = 0
            for line in final_count_lines:
                colon_index = line.rfind(":")
                if colon_index > 0:
                    count_str = line[colon_index + 1 :]
                    try:
                        count = int(count_str)
                    except ValueError:
                        continue
                    total_matches += count
                    file_count += 1

            output = {
                "mode": "count",
                "numFiles": file_count,
                "filenames": [],
                "content": "\n".join(final_count_lines),
                "numMatches": total_matches,
            }
            if applied_limit is not None:
                output["appliedLimit"] = applied_limit
            if offset > 0:
                output["appliedOffset"] = offset
            return ToolResult(data=output)

        # files_with_matches mode (default).
        # Sort by mtime desc; failed stats sort as 0. In tests, sort by filename for determinism.
        node_env_test = os.environ.get("NODE_ENV") == "test"

        async def _mtime_ms(file_path: str) -> float:
            try:
                st = await asyncio.to_thread(os.stat, file_path)
                return st.st_mtime * 1000
            except OSError:
                return 0.0

        mtimes = await asyncio.gather(*(_mtime_ms(r) for r in results))
        indexed = list(zip(results, mtimes, strict=True))

        if node_env_test:
            indexed.sort(key=lambda pair: pair[0])
        else:
            # Sort by mtime descending, filename ascending as tiebreaker.
            indexed.sort(key=lambda pair: pair[0])
            indexed.sort(key=lambda pair: pair[1], reverse=True)

        sorted_matches = [pair[0] for pair in indexed]

        final_matches, applied_limit = apply_head_limit(sorted_matches, head_limit_int, offset)
        relative_matches = [to_relative_path(m) for m in final_matches]

        output = {
            "mode": "files_with_matches",
            "filenames": relative_matches,
            "numFiles": len(relative_matches),
        }
        if applied_limit is not None:
            output["appliedLimit"] = applied_limit
        if offset > 0:
            output["appliedOffset"] = offset

        return ToolResult(data=output)


grep_tool = GrepTool()
