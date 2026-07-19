"""NotebookEdit tool — edit a single cell of a Jupyter ``.ipynb``.

``NotebookEditTool`` subclasses :class:`tabvis.tool.Tool` and is exported as the singleton
:data:`notebook_edit_tool`. Input validation uses a pydantic v2 ``BaseModel``
(:class:`NotebookEditInput`) with ``extra='forbid'``.

The tool is pure JSON manipulation of the ``.ipynb`` file: it reads the notebook, locates a cell
by ``cell_id`` (its actual ``id`` or a ``cell-N`` numeric index), then ``replace``/``insert``/
``delete``-s it, and writes the JSON back with 1-space indentation.

Implementation notes:

* Metadata-aware file reading (content + encoding + line endings), modification-time lookup,
  text writing, and safe JSON parsing are all implemented locally in this module. JSON
  encode/decode go through plain ``json.loads``/``json.dumps``; the parse in ``call`` is
  non-memoized since the notebook dict is mutated in place afterward.
* Inserted-cell ids are a random base36 string of up to 13 characters.
* ``readFileState`` is the per-call ``dict[str, {content,timestamp,offset,limit}]`` from
  :class:`tabvis.tool.ToolUseContext` (a cache keyed by absolute path); the Read-before-Edit guard
  and the post-write update both go through it.

Casing: Python identifiers are snake_case; the ``Output`` ``data`` dict and the tool_result block
keep their wire keys (``new_source``/``cell_id``/``cell_type``/``edit_mode``/``notebook_path``/...;
``tool_use_id``/``type``/``content``/``is_error``). The ``.ipynb`` JSON keys are the notebook
format's own wire keys (``cell_type``/``execution_count``/``nbformat``/...).
"""

from __future__ import annotations

import json
import os
import random
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext, ValidationResult
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.types.message import AssistantMessage
from tabvis.types.permissions import PermissionDecision
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import is_enoent
from tabvis.utils.file import get_file_modification_time, write_text_content

# ---------------------------------------------------------------------------
# tool name, description, and prompt
# ---------------------------------------------------------------------------

NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"

DESCRIPTION = "Replace the contents of a specific cell in a Jupyter notebook."

PROMPT = (
    "Completely replaces the contents of a specific cell in a Jupyter notebook (.ipynb file) "
    "with new source. Jupyter notebooks are interactive documents that combine code, text, and "
    "visualizations, commonly used for data analysis and scientific computing. The notebook_path "
    "parameter must be an absolute path, not a relative path. The cell_number is 0-indexed. Use "
    "edit_mode=insert to add a new cell at the index specified by cell_number. Use "
    "edit_mode=delete to delete the cell at the index specified by cell_number."
)


# ---------------------------------------------------------------------------
# input schema
# ---------------------------------------------------------------------------

CellType = Literal["code", "markdown"]
EditMode = Literal["replace", "insert", "delete"]


class NotebookEditInput(BaseModel):
    """Validated input for :data:`notebook_edit_tool`."""

    model_config = ConfigDict(extra="forbid")

    notebook_path: str = Field(
        description=(
            "The absolute path to the Jupyter notebook file to edit "
            "(must be absolute, not relative)"
        ),
    )
    cell_id: str | None = Field(
        default=None,
        description=(
            "The ID of the cell to edit. When inserting a new cell, the new cell will be "
            "inserted after the cell with this ID, or at the beginning if not specified."
        ),
    )
    new_source: str = Field(description="The new source for the cell")
    cell_type: CellType | None = Field(
        default=None,
        description=(
            "The type of the cell (code or markdown). If not specified, it defaults to the "
            "current cell type. If using edit_mode=insert, this is required."
        ),
    )
    edit_mode: EditMode | None = Field(
        default=None,
        description=(
            "The type of edit to make (replace, insert, delete). Defaults to replace."
        ),
    )


# ---------------------------------------------------------------------------
# local notebook/JSON/file-read helpers
# ---------------------------------------------------------------------------

_CELL_ID_RE = re.compile(r"^cell-(\d+)$")


def parse_cell_id(cell_id: str) -> int | None:
    """Parse a ``cell-N`` id into its numeric index; returns ``None`` for any other shape."""
    match = _CELL_ID_RE.match(cell_id)
    if match:
        try:
            return int(match.group(1), 10)
        except ValueError:
            return None
    return None


def _safe_parse_json(content: str | None) -> Any | None:
    """Parse JSON, returning ``None`` on any failure instead of raising.

    Strips a leading BOM before parsing — some tools (e.g. PowerShell) write UTF-8 files with a
    BOM prefix.
    """
    if not content:
        return None
    try:
        return json.loads(content.lstrip("﻿"))
    except (ValueError, TypeError):
        return None


def _detect_line_endings_for_string(content: str) -> str:
    """Detect whether ``content`` predominantly uses CRLF or LF line endings."""
    crlf_count = 0
    lf_count = 0
    for i, ch in enumerate(content):
        if ch == "\n":
            if i > 0 and content[i - 1] == "\r":
                crlf_count += 1
            else:
                lf_count += 1
    return "CRLF" if crlf_count > lf_count else "LF"


def _detect_encoding_for_path(file_path: str) -> str:
    """Detect file encoding by sniffing for a UTF-16LE BOM, else assume utf-8."""
    with open(file_path, "rb") as fh:
        head = fh.read(4096)
    if len(head) == 0:
        return "utf-8"
    if len(head) >= 2 and head[0] == 0xFF and head[1] == 0xFE:
        return "utf-16-le"
    return "utf-8"


def _read_file_sync_with_metadata(file_path: str) -> dict[str, str]:
    """Read a file's content, encoding, and line-ending style in one pass.

    Resolves through a symlink (logging at debug level), detects encoding, reads with that
    encoding, detects line endings from the raw head (before CRLF normalization erases the
    distinction), then normalizes ``\\r\\n`` -> ``\\n`` in the returned content.
    """
    resolved_path = file_path
    try:
        link_target = os.readlink(file_path)
        resolved_path = (
            link_target
            if os.path.isabs(link_target)
            else os.path.normpath(os.path.join(os.path.dirname(file_path), link_target))
        )
        log_for_debugging(f"Reading through symlink: {file_path} -> {resolved_path}")
    except OSError:
        # ENOENT (missing) or EINVAL (not a symlink) — read the path as-is.
        pass

    encoding = _detect_encoding_for_path(resolved_path)
    with open(resolved_path, encoding=encoding) as fh:
        raw = fh.read()
    line_endings = _detect_line_endings_for_string(raw[:4096])
    return {
        "content": raw.replace("\r\n", "\n"),
        "encoding": encoding,
        "lineEndings": line_endings,
    }


def _random_cell_id() -> str:
    """Generate a random base36 cell id, up to 13 characters."""
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(13))


def _resolve_full_path(notebook_path: str) -> str:
    """Resolve ``notebook_path`` to an absolute path, relative to the current working directory."""
    if os.path.isabs(notebook_path):
        return notebook_path
    return os.path.normpath(os.path.join(get_cwd(), notebook_path))


def _get_tool_use_summary(input_obj: Any) -> str | None:
    """Return the ``notebook_path`` from the tool input, or ``None``."""
    if input_obj is None:
        return None
    if isinstance(input_obj, dict):
        return input_obj.get("notebook_path")
    return getattr(input_obj, "notebook_path", None)


def _input_get(input_obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(input_obj, dict):
        return input_obj.get(key, default)
    value = getattr(input_obj, key, default)
    return default if value is None else value


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class NotebookEditTool(Tool):
    """``NotebookEdit`` — replace/insert/delete a cell in a Jupyter ``.ipynb``."""

    name = NOTEBOOK_EDIT_TOOL_NAME
    search_hint = "edit Jupyter notebook cells (.ipynb)"
    input_schema = NotebookEditInput
    max_result_size_chars = 100_000
    should_defer = True

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:
        return PROMPT

    def user_facing_name(self, input: Any | None = None) -> str:
        return "Edit Notebook"

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return _get_tool_use_summary(input)

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = _get_tool_use_summary(input)
        return f"Editing notebook {summary}" if summary else "Editing notebook"

    def get_path(self, input: Any) -> str | None:
        return _input_get(input, "notebook_path")

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        # No filesystem permission rules are configured for this tool, so always allow.
        return {"behavior": "allow", "updatedInput": input}

    async def validate_input(
        self, input: Any, context: ToolUseContext
    ) -> ValidationResult:
        notebook_path: str = _input_get(input, "notebook_path", "")
        cell_type = _input_get(input, "cell_type")
        cell_id = _input_get(input, "cell_id")
        edit_mode = _input_get(input, "edit_mode") or "replace"

        full_path = _resolve_full_path(notebook_path)

        # SECURITY: skip filesystem operations for UNC paths to prevent NTLM credential leaks.
        if full_path.startswith("\\\\") or full_path.startswith("//"):
            return ValidationResult(result=True)

        if os.path.splitext(full_path)[1] != ".ipynb":
            return ValidationResult(
                result=False,
                message=(
                    "File must be a Jupyter notebook (.ipynb file). For editing other file "
                    "types, use the FileEdit tool."
                ),
                error_code=2,
            )

        if edit_mode not in ("replace", "insert", "delete"):
            return ValidationResult(
                result=False,
                message="Edit mode must be replace, insert, or delete.",
                error_code=4,
            )

        if edit_mode == "insert" and not cell_type:
            return ValidationResult(
                result=False,
                message="Cell type is required when using edit_mode=insert.",
                error_code=5,
            )

        # Require Read-before-Edit (matches FileEditTool/FileWriteTool). Without this, the model
        # could edit a notebook it never saw, or edit against a stale view after an external
        # change — silent data loss.
        read_timestamp = context.read_file_state.get(full_path)
        if not read_timestamp:
            return ValidationResult(
                result=False,
                message="File has not been read yet. Read it first before writing to it.",
                error_code=9,
            )
        if get_file_modification_time(full_path) > read_timestamp["timestamp"]:
            return ValidationResult(
                result=False,
                message=(
                    "File has been modified since read, either by the user or by a linter. "
                    "Read it again before attempting to write it."
                ),
                error_code=10,
            )

        try:
            content = _read_file_sync_with_metadata(full_path)["content"]
        except OSError as e:
            if is_enoent(e):
                return ValidationResult(
                    result=False,
                    message="Notebook file does not exist.",
                    error_code=1,
                )
            raise

        notebook = _safe_parse_json(content)
        if not notebook:
            return ValidationResult(
                result=False,
                message="Notebook is not valid JSON.",
                error_code=6,
            )

        cells = notebook.get("cells", [])
        if not cell_id:
            if edit_mode != "insert":
                return ValidationResult(
                    result=False,
                    message="Cell ID must be specified when not inserting a new cell.",
                    error_code=7,
                )
        else:
            # First try to find the cell by its actual ID.
            cell_index = next(
                (i for i, cell in enumerate(cells) if cell.get("id") == cell_id),
                -1,
            )
            if cell_index == -1:
                # If not found, try to parse as a numeric index (cell-N format).
                parsed_cell_index = parse_cell_id(cell_id)
                if parsed_cell_index is not None:
                    if not (0 <= parsed_cell_index < len(cells)):
                        return ValidationResult(
                            result=False,
                            message=(
                                f"Cell with index {parsed_cell_index} does not exist in "
                                "notebook."
                            ),
                            error_code=7,
                        )
                else:
                    return ValidationResult(
                        result=False,
                        message=f'Cell with ID "{cell_id}" not found in notebook.',
                        error_code=8,
                    )

        return ValidationResult(result=True)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        cell_id = data.get("cell_id")
        edit_mode = data.get("edit_mode")
        new_source = data.get("new_source")
        error = data.get("error")

        if error:
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": error,
                "is_error": True,
            }
        if edit_mode == "replace":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Updated cell {cell_id} with {new_source}",
            }
        if edit_mode == "insert":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Inserted cell {cell_id} with {new_source}",
            }
        if edit_mode == "delete":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Deleted cell {cell_id}",
            }
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": "Unknown edit mode",
        }

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        notebook_path: str = args.notebook_path
        new_source: str = args.new_source
        cell_id: str | None = args.cell_id
        cell_type: str | None = args.cell_type
        original_edit_mode: str | None = args.edit_mode

        read_file_state = context.read_file_state

        full_path = _resolve_full_path(notebook_path)

        try:
            meta = _read_file_sync_with_metadata(full_path)
            file_content = meta["content"]
            encoding = meta["encoding"]
            line_endings = meta["lineEndings"]

            # Non-memoized parse (the notebook is mutated in place below).
            try:
                notebook = json.loads(file_content)
            except (ValueError, TypeError):
                return ToolResult(
                    data={
                        "new_source": new_source,
                        "cell_type": cell_type or "code",
                        "language": "python",
                        "edit_mode": "replace",
                        "error": "Notebook is not valid JSON.",
                        "cell_id": cell_id,
                        "notebook_path": full_path,
                        "original_file": "",
                        "updated_file": "",
                    }
                )

            cells = notebook["cells"]

            if not cell_id:
                # Default to inserting at the beginning if no cell_id is provided.
                cell_index = 0
            else:
                # First try to find the cell by its actual ID.
                cell_index = next(
                    (i for i, cell in enumerate(cells) if cell.get("id") == cell_id),
                    -1,
                )
                # If not found, try to parse as a numeric index (cell-N format).
                if cell_index == -1:
                    parsed_cell_index = parse_cell_id(cell_id)
                    if parsed_cell_index is not None:
                        cell_index = parsed_cell_index
                if original_edit_mode == "insert":
                    cell_index += 1  # Insert after the cell with this ID.

            # Convert replace to insert if trying to replace one past the end.
            edit_mode = original_edit_mode
            if edit_mode == "replace" and cell_index == len(cells):
                edit_mode = "insert"
                if not cell_type:
                    cell_type = "code"  # Default to code if no cell_type specified.

            language = (notebook.get("metadata", {}).get("language_info") or {}).get(
                "name"
            ) or "python"

            new_cell_id = None
            nbformat = notebook.get("nbformat", 0)
            nbformat_minor = notebook.get("nbformat_minor", 0)
            if nbformat > 4 or (nbformat == 4 and nbformat_minor >= 5):
                if edit_mode == "insert":
                    new_cell_id = _random_cell_id()
                elif cell_id is not None:
                    new_cell_id = cell_id

            if edit_mode == "delete":
                # Delete the specified cell.
                del cells[cell_index]
            elif edit_mode == "insert":
                if cell_type == "markdown":
                    new_cell = {
                        "cell_type": "markdown",
                        "id": new_cell_id,
                        "source": new_source,
                        "metadata": {},
                    }
                else:
                    new_cell = {
                        "cell_type": "code",
                        "id": new_cell_id,
                        "source": new_source,
                        "metadata": {},
                        "execution_count": None,
                        "outputs": [],
                    }
                # Insert the new cell.
                cells.insert(cell_index, new_cell)
            else:
                # Find the specified cell (validateInput ensures cell_index is in bounds).
                target_cell = cells[cell_index]
                target_cell["source"] = new_source
                if target_cell.get("cell_type") == "code":
                    # Reset execution count and clear outputs since cell was modified.
                    target_cell["execution_count"] = None
                    target_cell["outputs"] = []
                if cell_type and cell_type != target_cell.get("cell_type"):
                    target_cell["cell_type"] = cell_type

            # Write back to file (JSON.stringify(notebook, null, 1) == indent=1).
            ipynb_indent = 1
            updated_content = json.dumps(notebook, indent=ipynb_indent, ensure_ascii=False)
            write_text_content(full_path, updated_content, encoding, line_endings)

            # Update readFileState with post-write mtime (matches FileEditTool/FileWriteTool).
            # offset:None breaks FileReadTool's dedup match — without this, Read->NotebookEdit->
            # Read in the same millisecond would return the file_unchanged stub against stale
            # in-context content.
            read_file_state[full_path] = {
                "content": updated_content,
                "timestamp": get_file_modification_time(full_path),
                "offset": None,
                "limit": None,
            }

            return ToolResult(
                data={
                    "new_source": new_source,
                    "cell_type": cell_type or "code",
                    "language": language,
                    "edit_mode": edit_mode or "replace",
                    "cell_id": new_cell_id or None,
                    "error": "",
                    "notebook_path": full_path,
                    "original_file": file_content,
                    "updated_file": updated_content,
                }
            )
        except Exception as error:  # noqa: BLE001 - always return an error result, never raise
            message = str(error) if isinstance(error, Exception) and str(error) else None
            return ToolResult(
                data={
                    "new_source": new_source,
                    "cell_type": cell_type or "code",
                    "language": "python",
                    "edit_mode": "replace",
                    "error": message or "Unknown error occurred while editing notebook",
                    "cell_id": cell_id,
                    "notebook_path": full_path,
                    "original_file": "",
                    "updated_file": "",
                }
            )


# Singleton instance.
notebook_edit_tool = NotebookEditTool()
