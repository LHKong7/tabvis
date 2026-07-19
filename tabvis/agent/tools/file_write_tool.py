"""``Write`` tool — create or overwrite a file at an absolute path with given content.

Writes (creates or overwrites) a file at an absolute ``file_path`` with ``content``.
It is a :class:`Tool` subclass whose singleton instance is exported as
:data:`file_write_tool`, with a pydantic v2 :class:`FileWriteInput` (``extra='forbid'``)
input schema.

Behavioral notes:

* ``backfill_observable_input`` expands ``~``/relative ``file_path`` so hook allowlists
  can't be bypassed.
* ``validate_input`` rejects writes to files modified since the last read (staleness
  guard) and files that were never read (must Read first), keyed by the expanded path in
  ``context.read_file_state``.
* ``call`` performs the atomic read-modify-write: it re-reads disk inside the critical
  section, raises :data:`FILE_UNEXPECTEDLY_MODIFIED_ERROR` on a race, writes via
  ``write_text_content`` (always ``LF`` — a write is a full content replacement), then
  records the new view into ``read_file_state`` (``offset``/``limit`` cleared).
* Result data is ``{type:'create'|'update', filePath, content, structuredPatch, originalFile}``
  (camelCase wire keys — round-trips to the transcript / SDK output).

Deeper functionality outside this build's scope (VSCode diff notify,
diagnostics, file-history backup, skills discovery, git-diff, feature flags) is stubbed as
no-ops; the write + state-cache update + analytics events are complete.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.utils.cwd import get_cwd
from tabvis.utils.diff import count_lines_changed, get_patch_for_display
from tabvis.utils.errors import is_enoent
from tabvis.utils.file import get_file_modification_time, write_text_content
from tabvis.utils.path import expand_path

if TYPE_CHECKING:
    from tabvis.tool import ToolCallProgress
    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.message import AssistantMessage

# Tool identity.
FILE_WRITE_TOOL_NAME = "Write"
DESCRIPTION = "Write a file to the local filesystem."

# Name of the read tool, inlined here to avoid importing the FileReadTool prompt
# module just for one constant.
FILE_READ_TOOL_NAME = "Read"

FILE_UNEXPECTEDLY_MODIFIED_ERROR = (
    "File has been unexpectedly modified. Read it again before attempting to write it."
)


def _get_pre_read_instruction() -> str:
    return (
        f"\n- If this is an existing file, you MUST use the {FILE_READ_TOOL_NAME} tool first to "
        f"read the file's contents. This tool will fail if you did not read the file first."
    )


def get_write_tool_description() -> str:
    """Build the usage description text shown to the model for the Write tool."""
    return (
        "Writes a file to the local filesystem.\n"
        "\n"
        "Usage:\n"
        "- This tool will overwrite the existing file if there is one at the provided path."
        f"{_get_pre_read_instruction()}\n"
        "- Prefer the Edit tool for modifying existing files — it only sends the diff. Only "
        "use this tool to create new files or for complete rewrites.\n"
        "- NEVER create documentation files (*.md) or README files unless explicitly requested "
        "by the User.\n"
        "- Only use emojis if the user explicitly requests it. Avoid writing emojis to files "
        "unless asked."
    )


class FileWriteInput(BaseModel):
    """Validated input for the ``Write`` tool."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(
        description="The absolute path to the file to write (must be absolute, not relative)",
    )
    content: str = Field(description="The content to write to the file")


def _read_file_with_metadata(full_file_path: str) -> dict[str, Any]:
    """Read a file with metadata.

    Returns ``{content, encoding}`` where ``content`` is CRLF-normalized to ``\\n`` (matching
    ``read_file_state``'s normalized form) and ``encoding`` is the text codec for the file.

    Encoding detection and line-ending sampling are not supported. This build assumes UTF-8 text;
    the line-ending style is unused here because writes always normalize to LF.
    """
    with open(full_file_path, "rb") as fh:
        data = fh.read()
    content = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return {"content": content, "encoding": "utf-8"}


# --- no-op stubs (UI / non-headless side effects) ---------------------------------
# These stand in for functionality (skills discovery, diagnostics, file history,
# git diff, feature flags) that this build does not implement.


def _discover_and_activate_skills(full_file_path: str, cwd: str) -> None:
    # Skill discovery/activation is not implemented; this is a no-op.
    return None


def _diagnostic_before_file_edited(full_file_path: str) -> None:
    return None


def _file_history_enabled() -> bool:
    return False


def _maybe_fetch_git_diff(full_file_path: str) -> dict[str, Any] | None:
    # Feature-flagged git-diff computation is not implemented; never computes a diff.
    return None


def _log_file_operation(full_file_path: str, op_type: str) -> None:
    return None


def _ensure_parent_dir(dir_path: str) -> None:
    """Recursively create the parent directory before writing."""
    os.makedirs(dir_path, exist_ok=True)


class FileWriteTool(Tool):
    name = FILE_WRITE_TOOL_NAME
    search_hint = "create or overwrite files"
    input_schema = FileWriteInput
    max_result_size_chars = 100_000
    strict = True

    # isReadOnly false / isConcurrencySafe false — writes are neither read-only nor safe
    # to run concurrently.
    def is_read_only(self, input: Any) -> bool:
        return False

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    async def check_permissions(self, input: Any, context: ToolUseContext) -> Any:
        # PP-7: route writes through the filesystem policy adapter (protects config:/sensitive paths).
        from tabvis.policy.filesystem_adapter import evaluate_path

        path = input.file_path if hasattr(input, "file_path") else (input or {}).get("file_path", "")
        return evaluate_path("filesystem.write", path, context, input)

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:
        return get_write_tool_description()

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return self._summary(input)

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = self._summary(input)
        return f"Writing {summary}" if summary else "Writing file"

    @staticmethod
    def _summary(input: Any | None) -> str | None:
        if input is None:
            return None
        if isinstance(input, BaseModel):
            return getattr(input, "file_path", None)
        if isinstance(input, dict):
            return input.get("file_path")
        return getattr(input, "file_path", None)

    def is_result_truncated(self, output: Any) -> bool:
        return False

    def get_path(self, input: Any) -> str | None:
        if isinstance(input, BaseModel):
            return input.file_path
        return input.get("file_path") if isinstance(input, dict) else None

    def backfill_observable_input(self, input: dict[str, Any]) -> None:
        # hooks.mdx documents file_path as absolute; expand so hook allowlists can't be
        # bypassed via ~ or relative paths.
        fp = input.get("file_path")
        if isinstance(fp, str):
            input["file_path"] = expand_path(fp)

    def extract_search_text(self, out: Any) -> str | None:
        # Transcript render shows content (create) or a structured diff (update); the raw
        # content would be phantom-indexed in update mode. file_path is already indexed via
        # tool_use, so return empty.
        return ""

    async def prepare_permission_matcher(self, input: Any):
        file_path = input.file_path if isinstance(input, BaseModel) else input["file_path"]

        def _matcher(pattern: str) -> bool:
            # No configured rules in this build; exact-match fallback is sufficient.
            return pattern == file_path

        return _matcher

    async def validate_input(self, input: Any, context: ToolUseContext):
        from tabvis.tool import ValidationResult

        file_path = input.file_path if isinstance(input, BaseModel) else input["file_path"]
        full_file_path = expand_path(file_path)

        # SECURITY: skip filesystem ops for UNC paths to prevent NTLM credential leaks.
        if full_file_path.startswith("\\\\") or full_file_path.startswith("//"):
            return ValidationResult(result=True)

        try:
            file_stat = os.stat(full_file_path)
            file_mtime_ms = file_stat.st_mtime * 1000.0
        except OSError as e:
            if is_enoent(e):
                return ValidationResult(result=True)
            raise

        read_timestamp = context.read_file_state.get(full_file_path)
        if not read_timestamp or read_timestamp.get("isPartialView"):
            return ValidationResult(
                result=False,
                message="File has not been read yet. Read it first before writing to it.",
                error_code=2,
            )

        # Reuse mtime from the stat above to avoid a redundant stat call.
        last_write_time = int(file_mtime_ms)
        if last_write_time > read_timestamp.get("timestamp", 0):
            return ValidationResult(
                result=False,
                message=(
                    "File has been modified since read, either by the user or by a linter. "
                    "Read it again before attempting to write it."
                ),
                error_code=3,
            )

        return ValidationResult(result=True)

    async def call(
        self,
        args: FileWriteInput,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: ToolCallProgress | None = None,
    ) -> ToolResult[dict[str, Any]]:
        file_path = args.file_path
        content = args.content
        read_file_state = context.read_file_state

        full_file_path = expand_path(file_path)
        dir_path = os.path.dirname(full_file_path)

        # Skills discovery (fire-and-forget, non-blocking).
        cwd = get_cwd()
        _discover_and_activate_skills(full_file_path, cwd)

        _diagnostic_before_file_edited(full_file_path)

        # Ensure parent dir exists BEFORE the atomic read-modify-write section (and before
        # the write) so directory creation can't race with the staleness check below.
        _ensure_parent_dir(dir_path)

        if _file_history_enabled():
            pass

        # Load current state and confirm no changes since last read. Avoid async ops between
        # here and writing to disk to preserve atomicity.
        try:
            meta: dict[str, Any] | None = _read_file_with_metadata(full_file_path)
        except OSError as e:
            if is_enoent(e):
                meta = None
            else:
                raise

        if meta is not None:
            last_write_time = get_file_modification_time(full_file_path)
            last_read = read_file_state.get(full_file_path)
            if not last_read or last_write_time > last_read.get("timestamp", 0):
                # Timestamps can change without content changes (cloud sync, AV). For full
                # reads, compare content as a fallback to avoid false positives.
                is_full_read = bool(
                    last_read
                    and last_read.get("offset") is None
                    and last_read.get("limit") is None
                )
                if not is_full_read or meta["content"] != (last_read or {}).get("content"):
                    raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)

        enc = meta["encoding"] if meta is not None else "utf-8"
        old_content = meta["content"] if meta is not None else None

        # PP-7 hardening: re-check at the write point to close the TOCTOU gap (a path swapped to a
        # symlink into a protected area after check_permissions is caught here via realpath).
        from tabvis.policy.filesystem_adapter import enforce_write

        enforce_write(full_file_path, context)

        # A write is a full content replacement — honor the model's explicit line endings.
        # Always normalize to LF (do NOT preserve the old file's endings).
        write_text_content(full_file_path, content, enc, "LF")

        # Update read timestamp to invalidate stale writes.
        read_file_state[full_file_path] = {
            "content": content,
            "timestamp": get_file_modification_time(full_file_path),
            "offset": None,
            "limit": None,
        }

        git_diff = _maybe_fetch_git_diff(full_file_path)

        if old_content:
            patch = get_patch_for_display(
                file_path=file_path,
                file_contents=old_content,
                edits=[
                    {
                        "old_string": old_content,
                        "new_string": content,
                        "replace_all": False,
                    }
                ],
            )
            data: dict[str, Any] = {
                "type": "update",
                "filePath": file_path,
                "content": content,
                "structuredPatch": patch,
                "originalFile": old_content,
            }
            if git_diff:
                data["gitDiff"] = git_diff
            # Track lines added/removed, right before yielding the result.
            count_lines_changed(patch)
            _log_file_operation(full_file_path, "update")
            return ToolResult(data=data)

        data = {
            "type": "create",
            "filePath": file_path,
            "content": content,
            "structuredPatch": [],
            "originalFile": None,
        }
        if git_diff:
            data["gitDiff"] = git_diff
        # For new files, count all lines as additions, right before yielding the result.
        count_lines_changed([], content)
        _log_file_operation(full_file_path, "create")
        return ToolResult(data=data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        file_path = content["filePath"]
        result_type = content["type"]
        if result_type == "create":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"File created successfully at: {file_path}",
            }
        # 'update'
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": f"The file {file_path} has been updated successfully.",
        }


# Exported singleton instance used throughout the tool registry.
file_write_tool = FileWriteTool()
