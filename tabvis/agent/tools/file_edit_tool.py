"""``Edit`` tool — exact string replacement in files.

Performs an exact string replacement (``file_path``, ``old_string``, ``new_string``,
optional ``replace_all``) with well-defined uniqueness / ``replace_all`` / not-found
error semantics:

* ``old_string`` is matched verbatim (with a curly-quote-normalization fallback via
  :func:`find_actual_string`);
* if it occurs more than once and ``replace_all`` is ``False`` → validation error
  (errorCode 9) listing the match count;
* if it is not found → validation error (errorCode 8);
* an empty ``old_string`` creates a new file (or fills an empty existing one);
* the file must have been read first (tracked read-state lookup), and must not have
  been modified since (timestamp + full-read content fallback).

Diffs are produced via :mod:`tabvis.utils.diff` (``get_patch_from_contents``). The result
``data`` keeps its camelCase wire keys (``filePath``/``oldString``/``newString``/
``originalFile``/``structuredPatch``/``userModified``/``replaceAll``) because it
round-trips into the transcript and SDK output.

Casing convention: Python identifiers are snake_case; the result ``data`` dict and the
``tool_result`` block param keep their Anthropic/transcript wire keys.

Deeper functionality outside this build's scope is not supported (skills
discovery, diagnostics, VSCode notify, file history, git diff, settings-file
validation, permission rule matching).
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import (
    Tool,
    ToolResult,
    ToolUseContext,
    ValidationResult,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.diff import (
    convert_leading_tabs_to_spaces,
    count_lines_changed,
    get_patch_from_contents,
)
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.errors import is_enoent
from tabvis.utils.file import (
    FILE_NOT_FOUND_CWD_NOTE,
    find_similar_file,
    get_display_path,
    get_file_modification_time,
    is_compact_line_prefix_enabled,
    suggest_path_under_cwd,
    write_text_content,
)
from tabvis.utils.path import expand_path

# --- constants --------------------------------------------------------------------------

FILE_EDIT_TOOL_NAME = "Edit"

FILE_UNEXPECTEDLY_MODIFIED_ERROR = (
    "File has been unexpectedly modified. Read it again before attempting to write it."
)

NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"

# Name of the read tool referenced in the prompt text below.
FILE_READ_TOOL_NAME = "Read"

# Size guard on the file being edited (1 GiB) — prevents OOM on huge files.
MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024


# --- curly-quote normalization -----------------------------------------------------------
# Tabvis can't output curly quotes, so we define them as constants. We normalize curly
# quotes to straight quotes when applying edits, then re-apply the file's style.
LEFT_SINGLE_CURLY_QUOTE = "‘"
RIGHT_SINGLE_CURLY_QUOTE = "’"
LEFT_DOUBLE_CURLY_QUOTE = "“"
RIGHT_DOUBLE_CURLY_QUOTE = "”"


def normalize_quotes(value: str) -> str:
    """Replace curly quotes with straight quotes."""
    return (
        value.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def find_actual_string(file_content: str, search_string: str) -> str | None:
    """Find the substring in ``file_content`` matching ``search_string``.

    First tries an exact match; failing that, normalizes curly quotes on both sides
    and returns the *original* file substring at the matched offset (length ==
    ``len(search_string)``), so the edit operates on real file bytes. Returns
    ``None`` when not found.
    """
    # Exact match first.
    if search_string in file_content:
        return search_string

    # Quote-normalized fallback.
    normalized_search = normalize_quotes(search_string)
    normalized_file = normalize_quotes(file_content)

    search_index = normalized_file.find(normalized_search)
    if search_index != -1:
        # normalizeQuotes is a length-preserving 1:1 char substitution, so indices in
        # the normalized file map directly back to the original file.
        return file_content[search_index : search_index + len(search_string)]

    return None


def _is_opening_context(chars: list[str], index: int) -> bool:
    """Whether a quote at ``index`` opens a quotation."""
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in (
        " ",
        "\t",
        "\n",
        "\r",
        "(",
        "[",
        "{",
        "—",  # em dash
        "–",  # en dash
    )


def _apply_curly_double_quotes(value: str) -> str:
    chars = list(value)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == '"':
            result.append(
                LEFT_DOUBLE_CURLY_QUOTE
                if _is_opening_context(chars, i)
                else RIGHT_DOUBLE_CURLY_QUOTE
            )
        else:
            result.append(ch)
    return "".join(result)


def _apply_curly_single_quotes(value: str) -> str:
    chars = list(value)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == "'":
            prev = chars[i - 1] if i > 0 else None
            nxt = chars[i + 1] if i < len(chars) - 1 else None
            prev_is_letter = prev is not None and prev.isalpha()
            next_is_letter = nxt is not None and nxt.isalpha()
            if prev_is_letter and next_is_letter:
                # Apostrophe in a contraction (e.g. "don't") -> right single curly.
                result.append(RIGHT_SINGLE_CURLY_QUOTE)
            else:
                result.append(
                    LEFT_SINGLE_CURLY_QUOTE
                    if _is_opening_context(chars, i)
                    else RIGHT_SINGLE_CURLY_QUOTE
                )
        else:
            result.append(ch)
    return "".join(result)


def preserve_quote_style(old_string: str, actual_old_string: str, new_string: str) -> str:
    """Re-apply the file's curly-quote typography to ``new_string``.

    When ``old_string`` matched only via quote normalization (i.e. ``actual_old_string``
    differs and contains curly quotes), the same curly style is applied to ``new_string``
    so the edit preserves typography.
    """
    if old_string == actual_old_string:
        return new_string

    has_double = (
        LEFT_DOUBLE_CURLY_QUOTE in actual_old_string
        or RIGHT_DOUBLE_CURLY_QUOTE in actual_old_string
    )
    has_single = (
        LEFT_SINGLE_CURLY_QUOTE in actual_old_string
        or RIGHT_SINGLE_CURLY_QUOTE in actual_old_string
    )

    if not has_double and not has_single:
        return new_string

    result = new_string
    if has_double:
        result = _apply_curly_double_quotes(result)
    if has_single:
        result = _apply_curly_single_quotes(result)
    return result


def apply_edit_to_file(
    original_content: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> str:
    """Apply a single edit and return the updated content.

    ``replace_all`` replaces every occurrence, otherwise only the first. When deleting
    (``new_string == ''``) and ``old_string`` lacks a trailing newline but appears
    immediately before one in the file, the matched trailing ``\\n`` is consumed too so
    the deletion doesn't leave a blank line.

    ``new_string`` is always treated as a literal replacement (no ``$``-style
    backreference expansion), matching how ``str.replace`` already works.
    """
    if replace_all:

        def _f(content: str, search: str, replace: str) -> str:
            return content.replace(search, replace)
    else:

        def _f(content: str, search: str, replace: str) -> str:
            return content.replace(search, replace, 1)

    if new_string != "":
        return _f(original_content, old_string, new_string)

    strip_trailing_newline = (not old_string.endswith("\n")) and (
        (old_string + "\n") in original_content
    )

    if strip_trailing_newline:
        return _f(original_content, old_string + "\n", new_string)
    return _f(original_content, old_string, new_string)


def get_patch_for_edit(
    *,
    file_path: str,
    file_contents: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> dict[str, Any]:
    """Apply one edit and return ``{'patch': hunks, 'updated_file': str}``.

    Does not write to disk. Raises ``ValueError`` with a fixed message when the edit
    is a no-op (``'String not found in file. Failed to apply edit.'`` /
    ``'Original and edited file match exactly. Failed to apply edit.'``).

    The returned patch is for display only (leading tabs rendered as spaces).
    """
    # Special case for empty files with an empty no-op edit.
    if file_contents == "" and old_string == "" and new_string == "":
        return {"patch": [], "updated_file": ""}

    previous_content = file_contents
    if old_string == "":
        updated_file = new_string
    else:
        updated_file = apply_edit_to_file(
            file_contents, old_string, new_string, replace_all
        )

    if updated_file == previous_content:
        raise ValueError("String not found in file. Failed to apply edit.")

    if updated_file == file_contents:
        raise ValueError("Original and edited file match exactly. Failed to apply edit.")

    patch = get_patch_from_contents(
        file_path=file_path,
        old_content=convert_leading_tabs_to_spaces(file_contents),
        new_content=convert_leading_tabs_to_spaces(updated_file),
    )
    return {"patch": patch, "updated_file": updated_file}


def are_file_edits_inputs_equivalent(input1: dict[str, Any], input2: dict[str, Any]) -> bool:
    """Whether two single-edit inputs produce the same result."""
    if input1.get("file_path") != input2.get("file_path"):
        return False

    edits1 = input1.get("edits", [])
    edits2 = input2.get("edits", [])

    # Fast path: literal edit equality.
    if len(edits1) == len(edits2) and all(
        e1.get("old_string") == e2.get("old_string")
        and e1.get("new_string") == e2.get("new_string")
        and e1.get("replace_all") == e2.get("replace_all")
        for e1, e2 in zip(edits1, edits2, strict=False)
    ):
        return True

    # Semantic comparison (requires file read). Missing file -> empty content.
    file_content = ""
    try:
        with open(input1["file_path"], encoding="utf-8") as fh:
            file_content = fh.read().replace("\r\n", "\n")
    except OSError as error:
        if not is_enoent(error):
            raise

    return _are_file_edits_equivalent(edits1, edits2, file_content)


def _apply_edits(file_contents: str, edits: list[dict[str, Any]]) -> str:
    updated = file_contents
    for edit in edits:
        old_string = edit.get("old_string", "")
        new_string = edit.get("new_string", "")
        replace_all = bool(edit.get("replace_all", False))
        updated = new_string if old_string == "" else apply_edit_to_file(
            updated, old_string, new_string, replace_all
        )
    return updated


def _are_file_edits_equivalent(
    edits1: list[dict[str, Any]],
    edits2: list[dict[str, Any]],
    original_content: str,
) -> bool:
    """Apply both edit sets and compare the results."""
    if len(edits1) == len(edits2) and all(
        e1.get("old_string") == e2.get("old_string")
        and e1.get("new_string") == e2.get("new_string")
        and e1.get("replace_all") == e2.get("replace_all")
        for e1, e2 in zip(edits1, edits2, strict=False)
    ):
        return True

    result1: str | None = None
    error1: str | None = None
    result2: str | None = None
    error2: str | None = None

    try:
        result1 = _apply_edits(original_content, edits1)
    except Exception as e:  # noqa: BLE001 - broad by design: compare failures by message
        error1 = str(e)
    try:
        result2 = _apply_edits(original_content, edits2)
    except Exception as e:  # noqa: BLE001
        error2 = str(e)

    if error1 is not None and error2 is not None:
        return error1 == error2
    if error1 is not None or error2 is not None:
        return False
    return result1 == result2


# --- prompt -----------------------------------------------------------------------------


def _get_pre_read_instruction() -> str:
    return (
        f"\n- You must use your `{FILE_READ_TOOL_NAME}` tool at least once in the "
        "conversation before editing. This tool will error if you attempt an edit "
        "without reading the file. "
    )


def get_edit_tool_description() -> str:
    """Build the usage description text shown to the model for the Edit tool."""
    prefix_format = (
        "line number + tab"
        if is_compact_line_prefix_enabled()
        else "spaces + line number + arrow"
    )
    minimal_uniqueness_hint = ""
    return f"""Performs exact string replacements in files.

Usage:{_get_pre_read_instruction()}
- When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: {prefix_format}. Everything after that is the actual file content to match. Never include any part of the line number prefix in the old_string or new_string.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- Only use emojis if the user explicitly requests it. Avoid adding emojis to files unless asked.
- The edit will FAIL if `old_string` is not unique in the file. Either provide a larger string with more surrounding context to make it unique or use `replace_all` to change every instance of `old_string`.{minimal_uniqueness_hint}
- Use `replace_all` for replacing and renaming strings across the file. This parameter is useful if you want to rename a variable for instance."""  # noqa: E501


# --- input schema -------------------------------------------------------------------------


class FileEditInput(BaseModel):
    """Validated input for the Edit tool."""

    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="The absolute path to the file to modify")
    old_string: str = Field(description="The text to replace")
    new_string: str = Field(
        description="The text to replace it with (must be different from old_string)"
    )
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences of old_string (default false)",
    )


# --- file read with metadata --------------------------------------------------------------
# Reads content, detects encoding, and detects line endings in one pass so the Edit tool
# can operate correctly on non-UTF-8 files and preserve the original line-ending style.


def _detect_line_endings_for_string(content: str) -> str:
    """Detect whether ``content`` uses CRLF or LF line endings; returns 'CRLF' or 'LF'."""
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
    """Sniff the file's encoding from a UTF-16LE BOM; otherwise assume UTF-8."""
    with open(file_path, "rb") as fh:
        head = fh.read(4096)
    if len(head) == 0:
        return "utf-8"
    if len(head) >= 2 and head[0] == 0xFF and head[1] == 0xFE:
        return "utf-16-le"
    return "utf-8"


def _read_file_sync_with_metadata(file_path: str) -> dict[str, str]:
    """Read a file's content along with its encoding and line-ending style.

    Resolves through a symlink (logging at debug level), detects encoding, reads with
    that encoding, detects line endings from the raw head (before CRLF normalization),
    then normalizes ``\\r\\n`` -> ``\\n`` in the returned content.
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


def _read_file_for_edit(absolute_file_path: str) -> dict[str, Any]:
    """Read a file for editing — returns content/exists/encoding/endings."""
    try:
        meta = _read_file_sync_with_metadata(absolute_file_path)
        return {
            "content": meta["content"],
            "file_exists": True,
            "encoding": meta["encoding"],
            "line_endings": meta["lineEndings"],
        }
    except OSError as e:
        if is_enoent(e):
            return {
                "content": "",
                "file_exists": False,
                "encoding": "utf-8",
                "line_endings": "LF",
            }
        raise


# --- the tool --------------------------------------------------------------------------


def _get_tool_use_summary(input_data: dict[str, Any] | None) -> str | None:
    if input_data and input_data.get("file_path"):
        return get_display_path(input_data["file_path"])
    return None


class FileEditTool(Tool):
    """``Edit`` — exact string replacement in a file."""

    name = FILE_EDIT_TOOL_NAME
    search_hint = "modify file contents in place"
    max_result_size_chars = 100_000
    strict = True
    input_schema = FileEditInput

    async def check_permissions(self, input: Any, context: ToolUseContext) -> Any:
        # PP-7: route edits (writes) through the filesystem policy adapter.
        from tabvis.policy.filesystem_adapter import evaluate_path

        path = input.file_path if hasattr(input, "file_path") else (input or {}).get("file_path", "")
        return evaluate_path("filesystem.write", path, context, input)

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return "A tool for editing files"

    async def prompt(self, options: dict[str, Any]) -> str:
        return get_edit_tool_description()

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return _get_tool_use_summary(input if isinstance(input, dict) else None)

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = _get_tool_use_summary(input if isinstance(input, dict) else None)
        return f"Editing {summary}" if summary else "Editing file"

    def get_path(self, input: Any) -> str | None:
        return input.get("file_path") if isinstance(input, dict) else None

    def backfill_observable_input(self, input: dict[str, Any]) -> None:
        # hooks.mdx documents file_path as absolute; expand so hook allowlists can't be
        # bypassed via ~ or relative paths.
        fp = input.get("file_path")
        if isinstance(fp, str):
            input["file_path"] = expand_path(fp)

    def inputs_equivalent(self, a: Any, b: Any) -> bool:
        def _edits(d: Any) -> dict[str, Any]:
            d = d if isinstance(d, dict) else {}
            return {
                "file_path": d.get("file_path"),
                "edits": [
                    {
                        "old_string": d.get("old_string"),
                        "new_string": d.get("new_string"),
                        "replace_all": d.get("replace_all", False),
                    }
                ],
            }

        return are_file_edits_inputs_equivalent(_edits(a), _edits(b))

    async def validate_input(
        self, input: Any, context: ToolUseContext
    ) -> ValidationResult:
        """Validate the edit request.

        Returns a :class:`ValidationResult` carrying a message + errorCode; the
        orchestrator maps ``result=False`` to an ``ask``/deny decision.
        """
        data = input.model_dump() if isinstance(input, FileEditInput) else dict(input)
        file_path = data["file_path"]
        old_string = data["old_string"]
        new_string = data["new_string"]
        replace_all = bool(data.get("replace_all", False))

        full_file_path = expand_path(file_path)

        if old_string == new_string:
            return ValidationResult(
                result=False,
                message="No changes to make: old_string and new_string are exactly the same.",
                error_code=1,
            )

        # SECURITY: skip filesystem ops for UNC paths (NTLM credential leak guard).
        if full_file_path.startswith("\\\\") or full_file_path.startswith("//"):
            return ValidationResult(result=True)

        # Prevent OOM on multi-GB files.
        try:
            size = os.stat(full_file_path).st_size
            if size > MAX_EDIT_FILE_SIZE:
                return ValidationResult(
                    result=False,
                    message=(
                        f"File is too large to edit ({_format_file_size(size)}). "
                        f"Maximum editable file size is {_format_file_size(MAX_EDIT_FILE_SIZE)}."
                    ),
                    error_code=10,
                )
        except OSError as e:
            if not is_enoent(e):
                raise

        # Read the file as bytes first to detect encoding from the buffer.
        file_content: str | None
        try:
            with open(full_file_path, "rb") as fh:
                file_buffer = fh.read()
            encoding = (
                "utf-16-le"
                if len(file_buffer) >= 2
                and file_buffer[0] == 0xFF
                and file_buffer[1] == 0xFE
                else "utf-8"
            )
            file_content = file_buffer.decode(encoding).replace("\r\n", "\n")
        except OSError as e:
            if is_enoent(e):
                file_content = None
            else:
                raise

        # File doesn't exist.
        if file_content is None:
            # Empty old_string on a nonexistent file means new file creation — valid.
            if old_string == "":
                return ValidationResult(result=True)
            similar_filename = find_similar_file(full_file_path)
            cwd_suggestion = await suggest_path_under_cwd(full_file_path)
            message = f"File does not exist. {FILE_NOT_FOUND_CWD_NOTE} {get_cwd()}."
            if cwd_suggestion:
                message += f" Did you mean {cwd_suggestion}?"
            elif similar_filename:
                message += f" Did you mean {similar_filename}?"
            return ValidationResult(result=False, message=message, error_code=4)

        # File exists with empty old_string — only valid if the file is empty.
        if old_string == "":
            if file_content.strip() != "":
                return ValidationResult(
                    result=False,
                    message="Cannot create new file - file already exists.",
                    error_code=3,
                )
            return ValidationResult(result=True)

        if full_file_path.endswith(".ipynb"):
            return ValidationResult(
                result=False,
                message=(
                    f"File is a Jupyter Notebook. Use the {NOTEBOOK_EDIT_TOOL_NAME} "
                    "to edit this file."
                ),
                error_code=5,
            )

        read_timestamp = context.read_file_state.get(full_file_path)
        if not read_timestamp or read_timestamp.get("isPartialView"):
            return ValidationResult(
                result=False,
                message="File has not been read yet. Read it first before writing to it.",
                error_code=6,
            )

        # Check if file was modified since last read.
        last_write_time = get_file_modification_time(full_file_path)
        if last_write_time > read_timestamp.get("timestamp", 0):
            is_full_read = (
                read_timestamp.get("offset") is None
                and read_timestamp.get("limit") is None
            )
            if is_full_read and file_content == read_timestamp.get("content"):
                pass  # Content unchanged, safe to proceed.
            else:
                return ValidationResult(
                    result=False,
                    message=(
                        "File has been modified since read, either by the user or by a "
                        "linter. Read it again before attempting to write it."
                    ),
                    error_code=7,
                )

        file = file_content

        actual_old_string = find_actual_string(file, old_string)
        if not actual_old_string:
            return ValidationResult(
                result=False,
                message=f"String to replace not found in file.\nString: {old_string}",
                error_code=8,
            )

        matches = file.count(actual_old_string)

        if matches > 1 and not replace_all:
            return ValidationResult(
                result=False,
                message=(
                    f"Found {matches} matches of the string to replace, but replace_all "
                    "is false. To replace all occurrences, set replace_all to true. To "
                    "replace only one occurrence, please provide more context to uniquely "
                    f"identify the instance.\nString: {old_string}"
                ),
                error_code=9,
            )

        # Settings-file-specific validation (for edits to config files) is not performed
        # here; this is fine for the common case of editing non-settings files.

        return ValidationResult(result=True)

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        data = args.model_dump() if isinstance(args, FileEditInput) else dict(args)
        file_path = data["file_path"]
        old_string = data["old_string"]
        new_string = data["new_string"]
        replace_all = bool(data.get("replace_all", False))

        read_file_state = context.read_file_state
        user_modified = context.user_modified

        absolute_file_path = expand_path(file_path)

        # Skill discovery/activation for this path is a no-op in this build (skipped
        # under TABVIS_SIMPLE, and unconditionally elsewhere since it isn't wired up yet).
        _cwd = get_cwd()
        if not is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
            pass


        # Ensure parent directory exists before the atomic read-modify-write section.
        parent_dir = os.path.dirname(absolute_file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # File-history backup is not implemented; this is a no-op.

        # 2. Load current state and confirm no changes since last read.
        read_result = _read_file_for_edit(absolute_file_path)
        original_file_contents = read_result["content"]
        file_exists = read_result["file_exists"]
        encoding = read_result["encoding"]
        endings = read_result["line_endings"]

        if file_exists:
            last_write_time = get_file_modification_time(absolute_file_path)
            last_read = read_file_state.get(absolute_file_path)
            if not last_read or last_write_time > last_read.get("timestamp", 0):
                is_full_read = (
                    last_read is not None
                    and last_read.get("offset") is None
                    and last_read.get("limit") is None
                )
                content_unchanged = (
                    is_full_read
                    and original_file_contents == (last_read or {}).get("content")
                )
                if not content_unchanged:
                    raise RuntimeError(FILE_UNEXPECTEDLY_MODIFIED_ERROR)

        # 3. Handle quote normalization.
        actual_old_string = (
            find_actual_string(original_file_contents, old_string) or old_string
        )
        actual_new_string = preserve_quote_style(
            old_string, actual_old_string, new_string
        )

        # 4. Generate patch.
        patch_result = get_patch_for_edit(
            file_path=absolute_file_path,
            file_contents=original_file_contents,
            old_string=actual_old_string,
            new_string=actual_new_string,
            replace_all=replace_all,
        )
        patch = patch_result["patch"]
        updated_file = patch_result["updated_file"]

        # 5. Write to disk (re-check at the write point to close the TOCTOU gap — PP-7 hardening).
        from tabvis.policy.filesystem_adapter import enforce_write

        enforce_write(absolute_file_path, context)
        write_text_content(absolute_file_path, updated_file, encoding, endings)

        # 6. Update read timestamp to invalidate stale writes.
        read_file_state[absolute_file_path] = {
            "content": updated_file,
            "timestamp": get_file_modification_time(absolute_file_path),
            "offset": None,
            "limit": None,
        }

        # 7. Log events.
        count_lines_changed(patch)

        # Remote-mode event emission is skipped; this build doesn't set TABVIS_REMOTE.

        # 8. Result. Wire keys (camelCase) preserved — round-trips into the transcript.
        result_data = {
            "filePath": file_path,
            "oldString": actual_old_string,
            "newString": new_string,
            "originalFile": original_file_contents,
            "structuredPatch": patch,
            "userModified": user_modified if user_modified is not None else False,
            "replaceAll": replace_all,
        }
        return ToolResult(data=result_data)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content
        file_path = data.get("filePath")
        user_modified = data.get("userModified")
        replace_all = data.get("replaceAll")

        modified_note = (
            ".  The user modified your proposed changes before accepting them. "
            if user_modified
            else ""
        )

        if replace_all:
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": (
                    f"The file {file_path} has been updated{modified_note}. "
                    "All occurrences were successfully replaced."
                ),
            }

        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": (
                f"The file {file_path} has been updated successfully{modified_note}."
            ),
        }


# --- format_file_size --------------------------------------------------------------------
# Formats a byte count as a human-readable size string, e.g. 1536 -> "1.5KB".


def _format_file_size(size_in_bytes: int) -> str:
    kb = size_in_bytes / 1024
    if kb < 1:
        return f"{size_in_bytes} bytes"
    if kb < 1024:
        return f"{_trim_zero(kb)}KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{_trim_zero(mb)}MB"
    gb = mb / 1024
    return f"{_trim_zero(gb)}GB"


def _trim_zero(value: float) -> str:
    text = f"{value:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return text


# Singleton instance used throughout the tool registry.
file_edit_tool = FileEditTool()
