"""``Read`` tool — read a file from the local filesystem.

Reads a file from the local filesystem. Supports an optional line ``offset``/``limit`` window
and a ``pages`` parameter for PDFs. Text reads are the primary path and are fully implemented
(dedup against ``read_file_state`` + ``read_file_in_range`` + line-numbered
serialization + cyber-risk reminder + empty/short-file system-reminder). Image, PDF and notebook
reads require heavy native deps (sharp/poppler/nbformat) that are not supported in this build;
those branches are stubbed and raise a clear error while keeping the wire shapes
(`data['type']` discriminants) intact so ``map_tool_result_to_tool_result_block_param`` stays
well-formed.

``max_result_size_chars`` is unbounded — output is bounded by ``maxTokens``
(``validate_content_tokens``); persisting a Read result to a file the model reads back is circular.
``isReadOnly`` / ``isConcurrencySafe`` are both ``True``.

Casing: Python identifiers snake_case; the ``data``/``file`` payload dicts and the
``tool_result`` block keep their wire keys (``filePath``, ``numLines``, ``startLine``,
``totalLines``, ``tool_use_id``, ``media_type``) so they round-trip to the transcript / API.
"""

# ruff: noqa: E501
from __future__ import annotations

import math
import ntpath
import os
import posixpath
from datetime import UTC
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ValidationResult
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.errors import get_errno_code, is_enoent
from tabvis.utils.file import (
    FILE_NOT_FOUND_CWD_NOTE,
    add_line_numbers,
    find_similar_file,
    get_file_modification_time_async,
    suggest_path_under_cwd,
)
from tabvis.utils.model.model import get_canonical_name, get_main_loop_model
from tabvis.utils.path import expand_path
from tabvis.utils.read_file_in_range import (
    FileTooLargeError,  # noqa: F401 - re-exported for parity with callers
    read_file_in_range,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from tabvis.tool import ToolCallProgress, ToolUseContext
    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.message import AssistantMessage


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FILE_READ_TOOL_NAME = "Read"

DESCRIPTION = "Read a file from the local filesystem."

FILE_UNCHANGED_STUB = (
    "File unchanged since last read. The content from the earlier Read tool_result in this "
    "conversation is still current — refer to that instead of re-reading."
)

MAX_LINES_TO_READ = 2000

LINE_FORMAT_INSTRUCTION = (
    "- Results are returned using cat -n format, with line numbers starting at 1"
)

OFFSET_INSTRUCTION_DEFAULT = (
    "- You can optionally specify a line offset and limit (especially handy for long files), "
    "but it's recommended to read the whole file by not providing these parameters"
)

OFFSET_INSTRUCTION_TARGETED = (
    "- When you already know which part of the file you need, only read that part. This can be "
    "important for larger files."
)

# PDF page-range limits.
PDF_MAX_PAGES_PER_READ = 20
PDF_AT_MENTION_INLINE_THRESHOLD = 10
PDF_EXTRACT_SIZE_THRESHOLD = 3 * 1024 * 1024

# 0.25 MB / 25000 tokens — the default file-reading limits. GrowthBook overrides + env
# override (TABVIS_FILE_READ_MAX_OUTPUT_TOKENS) handled below.
MAX_OUTPUT_SIZE = int(0.25 * 1024 * 1024)
DEFAULT_MAX_OUTPUT_TOKENS = 25000

CYBER_RISK_MITIGATION_REMINDER = (
    "\n\n<system-reminder>\nWhenever you read a file, you should consider whether it would be "
    "considered malware. You CAN and SHOULD provide analysis of malware, what it is doing. But "
    "you MUST refuse to improve or augment the code. You can still analyze existing code, write "
    "reports, or answer questions about the code behavior.\n</system-reminder>\n"
)

# Models where cyber risk mitigation should be skipped.
MITIGATION_EXEMPT_MODELS = frozenset({"claude-opus-4-6"})

# Common image extensions (bare, no leading dot).
IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})

# Document extensions treated as PDFs (bare, no leading dot).
DOCUMENT_EXTENSIONS = frozenset({"pdf"})

# Binary file extensions (with leading dot, lowercased).
BINARY_EXTENSIONS = frozenset(
    {
        # Images
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tiff", ".tif",
        # Videos
        ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".m4v", ".mpeg", ".mpg",
        # Audio
        ".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a", ".wma", ".aiff", ".opus",
        # Archives
        ".zip", ".tar", ".gz", ".bz2", ".7z", ".rar", ".xz", ".z", ".tgz", ".iso",
        # Executables / libs
        ".exe", ".dll", ".so", ".dylib", ".bin", ".o", ".a", ".obj", ".lib", ".app",
        ".msi", ".deb", ".rpm",
        # Documents
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
        # Fonts
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        # Compiled
        ".pyc", ".pyo", ".class", ".jar", ".war", ".ear", ".node", ".wasm", ".rlib",
        # Databases
        ".sqlite", ".sqlite3", ".db", ".mdb", ".idx",
        # Design
        ".psd", ".ai", ".eps", ".sketch", ".fig", ".xd", ".blend", ".3ds", ".max",
        ".swf", ".fla",
        # Misc
        ".lockb", ".dat", ".data",
    }
)

# Device files that would hang the process: infinite output or blocking input.
BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero", "/dev/random", "/dev/urandom", "/dev/full",
        "/dev/stdin", "/dev/tty", "/dev/console",
        "/dev/stdout", "/dev/stderr",
        "/dev/fd/0", "/dev/fd/1", "/dev/fd/2",
    }
)

# Narrow no-break space (U+202F) used by some macOS versions in screenshot filenames.
_THIN_SPACE = " "


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MaxFileReadTokenExceededError(Exception):
    """Raised when file content exceeds the maximum allowed token count."""

    def __init__(self, token_count: int, max_tokens: int) -> None:
        self.token_count = token_count
        self.max_tokens = max_tokens
        super().__init__(
            f"File content ({token_count} tokens) exceeds maximum allowed tokens "
            f"({max_tokens}). Use offset and limit parameters to read specific portions of the "
            f"file, or search for specific content instead of reading the whole file."
        )
        self.name = "MaxFileReadTokenExceededError"


# ---------------------------------------------------------------------------
# Stubbed deep dependencies (heavy native modules not supported in this build)
# ---------------------------------------------------------------------------


def has_binary_extension(file_path: str) -> bool:
    """Whether ``file_path``'s extension is a known binary format."""
    dot = file_path.rfind(".")
    ext = file_path[dot:].lower() if dot != -1 else ""
    return ext in BINARY_EXTENSIONS


def is_pdf_extension(ext: str) -> bool:
    """Whether ``ext`` (with or without leading dot) denotes a PDF."""
    normalized = ext[1:] if ext.startswith(".") else ext
    return normalized.lower() in DOCUMENT_EXTENSIONS


def is_pdf_supported() -> bool:
    """Whether the current model supports reading PDFs."""
    return "claude-3-haiku" not in get_main_loop_model().lower()


def parse_pdf_page_range(pages: str) -> dict[str, float] | None:
    """Parse a PDF page-range string like "3", "10-20", or "5-" into ``{firstPage, lastPage}``.

    Returns ``None`` when the string is malformed. ``lastPage`` is ``math.inf`` for an
    open-ended ``"N-"`` range.
    """
    trimmed = pages.strip()
    if not trimmed:
        return None

    if trimmed.endswith("-"):
        first = _parse_int(trimmed[:-1])
        if first is None or first < 1:
            return None
        return {"firstPage": float(first), "lastPage": math.inf}

    dash_index = trimmed.find("-")
    if dash_index == -1:
        page = _parse_int(trimmed)
        if page is None or page < 1:
            return None
        return {"firstPage": float(page), "lastPage": float(page)}

    first = _parse_int(trimmed[:dash_index])
    last = _parse_int(trimmed[dash_index + 1 :])
    if first is None or last is None or first < 1 or last < 1 or last < first:
        return None
    return {"firstPage": float(first), "lastPage": float(last)}


def _parse_int(text: str) -> int | None:
    """``parseInt(text, 10)`` semantics: leading digits, NaN-as-None."""
    text = text.strip()
    match = ""
    for i, ch in enumerate(text):
        if i == 0 and ch in "+-":
            match += ch
            continue
        if ch.isdigit():
            match += ch
        else:
            break
    if match in ("", "+", "-"):
        return None
    return int(match)


def get_canonical_name_for_mitigation() -> str:
    return get_canonical_name(get_main_loop_model())


def should_include_file_read_mitigation() -> bool:
    """Whether the cyber-risk mitigation reminder should be appended to this read's result."""
    return get_canonical_name_for_mitigation() not in MITIGATION_EXEMPT_MODELS


# PDF page extraction, image resizing, precise token estimation, skill discovery,
# feature-flag lookups, file-operation analytics, and memory-age tracking are not
# implemented in this build; the corresponding branches are stubbed below.


def _read_notebook_stub(resolved_file_path: str) -> Any:
    raise NotImplementedError(
        "Notebook (.ipynb) reading is not yet implemented (requires notebook parsing support)."
    )


def _read_image_stub(resolved_file_path: str, max_tokens: int) -> Any:
    raise NotImplementedError(
        "Image reading is not yet implemented (requires native image processing support)."
    )


def _read_pdf_stub(resolved_file_path: str, pages: str | None) -> Any:
    raise NotImplementedError(
        "PDF reading is not yet implemented (requires PDF text extraction support)."
    )


def _create_user_message(content: Any, *, is_meta: bool = False) -> dict[str, Any]:
    """Minimal user-message builder.

    Only the fields the image/PDF supplemental-content path needs are produced here.
    """
    import uuid as _uuid
    from datetime import datetime

    msg: dict[str, Any] = {
        "type": "user",
        "message": {"role": "user", "content": content or "(no content)"},
        "uuid": str(_uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if is_meta:
        msg["isMeta"] = True
    return msg


def get_default_file_reading_limits() -> dict[str, Any]:
    """Default file-reading limits — env override plus hardcoded defaults.

    Only the env var (``TABVIS_FILE_READ_MAX_OUTPUT_TOKENS``) takes precedence over the hardcoded
    default.
    """
    env = os.environ.get("TABVIS_FILE_READ_MAX_OUTPUT_TOKENS")
    max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    if env:
        try:
            parsed = int(env)
            if parsed > 0:
                max_tokens = parsed
        except ValueError:
            pass
    return {
        "maxSizeBytes": MAX_OUTPUT_SIZE,
        "maxTokens": max_tokens,
        "includeMaxSizeInPrompt": None,
        "targetedRangeNudge": None,
    }


def _format_file_size(size_in_bytes: int) -> str:
    """Format a byte count as a human-readable size string (see read_file_in_range)."""
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
    return text[:-2] if text.endswith(".0") else text


# ---------------------------------------------------------------------------
# Path helpers (device blocking + macOS screenshot alternate)
# ---------------------------------------------------------------------------


def is_blocked_device_path(file_path: str) -> bool:
    """Whether ``file_path`` is a device file that would hang the process."""
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    if file_path.startswith("/proc/") and (
        file_path.endswith("/fd/0")
        or file_path.endswith("/fd/1")
        or file_path.endswith("/fd/2")
    ):
        return True
    return False


def get_alternate_screenshot_path(file_path: str) -> str | None:
    """Return the alternate-space screenshot path to try, or ``None``.

    macOS screenshot filenames put either a regular space or a thin space (U+202F) before
    AM/PM depending on the OS version.
    """
    import re

    filename = os.path.basename(file_path)
    match = re.match(r"^(.+)([  ])(AM|PM)(\.png)$", filename)
    if not match:
        return None
    current_space = match.group(2)
    alternate_space = _THIN_SPACE if current_space == " " else " "
    return file_path.replace(
        f"{current_space}{match.group(3)}{match.group(4)}",
        f"{alternate_space}{match.group(3)}{match.group(4)}",
    )


def detect_session_file_type(file_path: str) -> str | None:
    """Best-effort session-file-type detection.

    Uses ``~/.tabvis`` as the config home directory so the normalized-path heuristics apply.
    """
    config_dir = os.path.join(os.path.expanduser("~"), ".tabvis")
    if not file_path.startswith(config_dir):
        return None
    # Normalize Windows separators to posix for consistent matching.
    normalized = file_path.replace(ntpath.sep, posixpath.sep)
    if "/session-memory/" in normalized and normalized.endswith(".md"):
        return "session_memory"
    if "/projects/" in normalized and normalized.endswith(".jsonl"):
        return "session_transcript"
    return None


# ---------------------------------------------------------------------------
# Input / output schemas
# ---------------------------------------------------------------------------


class FileReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(description="The absolute path to the file to read")
    offset: int | None = Field(
        default=None,
        ge=0,
        description=(
            "The line number to start reading from. Only provide if the file is too large to "
            "read at once"
        ),
    )
    limit: int | None = Field(
        default=None,
        gt=0,
        description=(
            "The number of lines to read. Only provide if the file is too large to read at once."
        ),
    )
    pages: str | None = Field(
        default=None,
        description=(
            f'Page range for PDF files (e.g., "1-5", "3", "10-20"). Only applicable to PDF '
            f"files. Maximum {PDF_MAX_PAGES_PER_READ} pages per request."
        ),
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _format_file_lines(file: dict[str, Any]) -> str:
    """Format the file's content with line numbers, matching ``cat -n`` style."""
    return add_line_numbers(file["content"], file["startLine"])


def render_prompt_template(
    line_format: str, max_size_instruction: str, offset_instruction: str
) -> str:
    """Render the tool's usage description sent to the model."""
    pdf_line = (
        "\n- This tool can read PDF files (.pdf). For large PDFs (more than 10 pages), you "
        "MUST provide the pages parameter to read specific page ranges (e.g., pages: \"1-5\"). "
        "Reading a large PDF without the pages parameter will fail. Maximum 20 pages per request."
        if is_pdf_supported()
        else ""
    )
    return (
        "Reads a file from the local filesystem. You can access any file directly by using "
        "this tool.\n"
        "Assume this tool is able to read all files on the machine. If the User provides a path "
        "to a file assume that path is valid. It is okay to read a file that does not exist; an "
        "error will be returned.\n\n"
        "Usage:\n"
        "- The file_path parameter must be an absolute path, not a relative path\n"
        f"- By default, it reads up to {MAX_LINES_TO_READ} lines starting from the beginning of "
        f"the file{max_size_instruction}\n"
        f"{offset_instruction}\n"
        f"{line_format}\n"
        "- This tool allows Tabvis to read images (eg PNG, JPG, etc). When reading an image file "
        "the contents are presented visually as Tabvis is a multimodal LLM."
        f"{pdf_line}\n"
        "- This tool can read Jupyter notebooks (.ipynb files) and returns all cells with their "
        "outputs, combining code, text, and visualizations.\n"
        "- This tool can only read files, not directories. To read a directory, use an ls "
        "command via the Bash tool.\n"
        "- You will regularly be asked to read screenshots. If the user provides a path to a "
        "screenshot, ALWAYS use this tool to view the file at the path. This tool will work with "
        "all temporary file paths.\n"
        "- If you read a file that exists but has empty contents you will receive a system "
        "reminder warning in place of file contents."
    )


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


class FileReadTool(Tool):
    name = FILE_READ_TOOL_NAME
    search_hint = "read files, images, PDFs, notebooks"
    # Output is bounded by maxTokens (validate_content_tokens). Persisting to a file the model
    # reads back with Read is circular — never persist.
    max_result_size_chars = float("inf")
    strict = True
    input_schema = FileReadInput

    # --- discovery / classification ---
    def is_concurrency_safe(self, input: Any) -> bool:  # noqa: ARG002
        return True

    def is_read_only(self, input: Any) -> bool:  # noqa: ARG002
        return True

    def is_search_or_read_command(self, input: Any) -> dict[str, bool] | None:  # noqa: ARG002
        return {"isSearch": False, "isRead": True}

    def get_path(self, input: Any) -> str:
        file_path = _input_get(input, "file_path")
        return file_path or get_cwd()

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        if input is None:
            return None
        return _input_get(input, "file_path") or None

    def get_activity_description(self, input: Any | None) -> str | None:
        summary = self.get_tool_use_summary(input)
        return f"Reading {summary}" if summary else "Reading file"

    def extract_search_text(self, out: Any) -> str:  # noqa: ARG002
        # UI renders only summary chrome; the content goes only to the model serialization.
        return ""

    def backfill_observable_input(self, input: dict[str, Any]) -> None:
        # hooks.mdx documents file_path as absolute; expand so hook allowlists can't be
        # bypassed via ~ or relative paths.
        if isinstance(input.get("file_path"), str):
            input["file_path"] = expand_path(input["file_path"])

    async def prepare_permission_matcher(
        self, input: Any
    ) -> Callable[[str], bool] | None:
        file_path = _input_get(input, "file_path")
        # Exact-match fallback keeps the matcher functional in this build.
        return lambda pattern: pattern == file_path

    async def check_permissions(self, input: Any, context: ToolUseContext) -> Any:
        # Allow by default (no configured read rules); deny rules are enforced in
        # validate_input via the same path-based check below.
        return {"behavior": "allow", "updatedInput": input}

    async def description(self, input: Any, options: dict[str, Any]) -> str:  # noqa: ARG002
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:  # noqa: ARG002
        limits = get_default_file_reading_limits()
        max_size_instruction = (
            f". Files larger than {_format_file_size(limits['maxSizeBytes'])} will return an "
            "error; use offset and limit for larger files"
            if limits.get("includeMaxSizeInPrompt")
            else ""
        )
        offset_instruction = (
            OFFSET_INSTRUCTION_TARGETED
            if limits.get("targetedRangeNudge")
            else OFFSET_INSTRUCTION_DEFAULT
        )
        return render_prompt_template(
            LINE_FORMAT_INSTRUCTION, max_size_instruction, offset_instruction
        )

    # --- validation (pure string parsing + path checks, no file I/O) ---
    async def validate_input(self, input: Any, context: ToolUseContext) -> ValidationResult:  # noqa: ARG002
        file_path = _input_get(input, "file_path")
        pages = _input_get(input, "pages")

        if pages is not None:
            parsed = parse_pdf_page_range(pages)
            if not parsed:
                return ValidationResult(
                    result=False,
                    message=(
                        f'Invalid pages parameter: "{pages}". Use formats like "1-5", "3", or '
                        '"10-20". Pages are 1-indexed.'
                    ),
                    error_code=7,
                )
            range_size = (
                PDF_MAX_PAGES_PER_READ + 1
                if parsed["lastPage"] == math.inf
                else int(parsed["lastPage"] - parsed["firstPage"] + 1)
            )
            if range_size > PDF_MAX_PAGES_PER_READ:
                return ValidationResult(
                    result=False,
                    message=(
                        f'Page range "{pages}" exceeds maximum of {PDF_MAX_PAGES_PER_READ} pages '
                        "per request. Please use a smaller range."
                    ),
                    error_code=8,
                )

        full_file_path = expand_path(file_path)

        # configured read deny rules → no deny match; this preserves the rule ordering.

        # SECURITY: UNC path check (no I/O).
        is_unc_path = full_file_path.startswith("\\\\") or full_file_path.startswith("//")
        if is_unc_path:
            return ValidationResult(result=True)

        # Binary extension check (string check on extension only, no I/O). PDF, images and SVG
        # are excluded — this tool renders them natively.
        dot = full_file_path.rfind(".")
        ext = full_file_path[dot:].lower() if dot != -1 else ""
        if (
            has_binary_extension(full_file_path)
            and not is_pdf_extension(ext)
            and ext[1:] not in IMAGE_EXTENSIONS
        ):
            return ValidationResult(
                result=False,
                message=(
                    f"This tool cannot read binary files. The file appears to be a binary {ext} "
                    "file. Please use appropriate tools for binary file analysis."
                ),
                error_code=4,
            )

        # Block specific device files that would hang (no I/O).
        if is_blocked_device_path(full_file_path):
            return ValidationResult(
                result=False,
                message=(
                    f"Cannot read '{file_path}': this device file would block or produce "
                    "infinite output."
                ),
                error_code=9,
            )

        return ValidationResult(result=True)

    # --- the read ---
    async def call(
        self,
        args: FileReadInput,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn | None = None,  # noqa: ARG002
        parent_message: AssistantMessage | None = None,
        on_progress: ToolCallProgress | None = None,  # noqa: ARG002
    ) -> ToolResult[Any]:
        file_path = args.file_path
        # Defaults: offset=1, limit=None.
        offset = args.offset if args.offset is not None else 1
        limit = args.limit
        pages = args.pages

        read_file_state = context.read_file_state
        file_reading_limits = context.file_reading_limits

        defaults = get_default_file_reading_limits()
        max_size_bytes = (
            file_reading_limits.get("maxSizeBytes")
            if file_reading_limits and file_reading_limits.get("maxSizeBytes") is not None
            else defaults["maxSizeBytes"]
        )
        max_tokens = (
            file_reading_limits.get("maxTokens")
            if file_reading_limits and file_reading_limits.get("maxTokens") is not None
            else defaults["maxTokens"]
        )

        if file_reading_limits is not None:
            pass

        dot = file_path.rfind(".")
        ext = file_path[dot + 1 :].lower() if dot != -1 else ""
        full_file_path = expand_path(file_path)

        # Dedup: same exact range, file unchanged on disk → return a stub.
        existing_state = _state_get(read_file_state, full_file_path)
        if (
            existing_state
            and not existing_state.get("isPartialView")
            and existing_state.get("offset") is not None
        ):
            range_match = (
                existing_state.get("offset") == offset
                and existing_state.get("limit") == limit
            )
            if range_match:
                try:
                    mtime_ms = await get_file_modification_time_async(full_file_path)
                    if mtime_ms == existing_state.get("timestamp"):
                        return ToolResult(
                            data={
                                "type": "file_unchanged",
                                "file": {"filePath": file_path},
                            }
                        )
                except OSError:
                    pass  # stat failed — fall through to full read

        # Skill discovery is not implemented in this build; this is a no-op regardless
        # of TABVIS_SIMPLE.
        _ = is_env_truthy(os.environ.get("TABVIS_SIMPLE"))

        message_id = None
        if parent_message is not None:
            try:
                message_id = parent_message.get("message", {}).get("id")  # type: ignore[union-attr]
            except (AttributeError, TypeError):
                message_id = None

        try:
            return await _call_inner(
                file_path,
                full_file_path,
                full_file_path,
                ext,
                offset,
                limit,
                pages,
                max_size_bytes,
                max_tokens,
                read_file_state,
                context,
                message_id,
            )
        except (OSError, FileNotFoundError) as error:
            code = get_errno_code(error)
            if code == "ENOENT" or isinstance(error, FileNotFoundError):
                alt_path = get_alternate_screenshot_path(full_file_path)
                if alt_path:
                    try:
                        return await _call_inner(
                            file_path,
                            full_file_path,
                            alt_path,
                            ext,
                            offset,
                            limit,
                            pages,
                            max_size_bytes,
                            max_tokens,
                            read_file_state,
                            context,
                            message_id,
                        )
                    except (OSError, FileNotFoundError) as alt_error:
                        if not is_enoent(alt_error) and not isinstance(
                            alt_error, FileNotFoundError
                        ):
                            raise

                similar_filename = find_similar_file(full_file_path)
                cwd_suggestion = await suggest_path_under_cwd(full_file_path)
                message = f"File does not exist. {FILE_NOT_FOUND_CWD_NOTE} {get_cwd()}."
                if cwd_suggestion:
                    message += f" Did you mean {cwd_suggestion}?"
                elif similar_filename:
                    message += f" Did you mean {similar_filename}?"
                raise FileNotFoundError(message) from error
            raise

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content
        data_type = data["type"]

        if data_type == "image":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "data": data["file"]["base64"],
                            "media_type": data["file"]["type"],
                        },
                    }
                ],
            }

        if data_type == "notebook":
            # a minimal text result referencing the cell count so the block stays well-formed.
            cells = data["file"].get("cells", [])
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Notebook read: {data['file']['filePath']} ({len(cells)} cells)",
            }

        if data_type == "pdf":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": (
                    f"PDF file read: {data['file']['filePath']} "
                    f"({_format_file_size(data['file']['originalSize'])})"
                ),
            }

        if data_type == "parts":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": (
                    f"PDF pages extracted: {data['file']['count']} page(s) from "
                    f"{data['file']['filePath']} "
                    f"({_format_file_size(data['file']['originalSize'])})"
                ),
            }

        if data_type == "file_unchanged":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": FILE_UNCHANGED_STUB,
            }

        if data_type == "text":
            file = data["file"]
            if file["content"]:
                content_str = (
                    _format_file_lines(file)
                    + (CYBER_RISK_MITIGATION_REMINDER if should_include_file_read_mitigation() else "")
                )
            elif file["totalLines"] == 0:
                content_str = (
                    "<system-reminder>Warning: the file exists but the contents are empty."
                    "</system-reminder>"
                )
            else:
                content_str = (
                    "<system-reminder>Warning: the file exists but is shorter than the provided "
                    f"offset ({file['startLine']}). The file has {file['totalLines']} lines."
                    "</system-reminder>"
                )
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": content_str,
            }

        raise ValueError(f"Unknown FileReadTool data type: {data_type!r}")


# ---------------------------------------------------------------------------
# Inner call (text path fully implemented; notebook/image/pdf stubbed)
# ---------------------------------------------------------------------------


async def _call_inner(
    file_path: str,
    full_file_path: str,
    resolved_file_path: str,
    ext: str,
    offset: int,
    limit: int | None,
    pages: str | None,
    max_size_bytes: int,
    max_tokens: int,
    read_file_state: Any,
    context: ToolUseContext,
    message_id: str | None,  # noqa: ARG001
) -> ToolResult[Any]:
    # --- Notebook ---
    if ext == "ipynb":
        _read_notebook_stub(resolved_file_path)  # raises NotImplementedError

    # --- Image ---
    if ext in IMAGE_EXTENSIONS:
        _read_image_stub(resolved_file_path, max_tokens)  # raises NotImplementedError

    # --- PDF ---
    if is_pdf_extension(ext):
        _read_pdf_stub(resolved_file_path, pages)  # raises NotImplementedError

    # --- Text file (single async read via read_file_in_range) ---
    line_offset = 0 if offset == 0 else offset - 1
    result = await read_file_in_range(
        resolved_file_path,
        line_offset,
        limit,
        max_size_bytes if limit is None else None,
        context.abort_controller.signal,
    )
    content = result["content"]
    line_count = result["lineCount"]
    total_lines = result["totalLines"]
    mtime_ms = result["mtimeMs"]

    await _validate_content_tokens(content, ext, max_tokens)

    _state_set(
        read_file_state,
        full_file_path,
        {
            "content": content,
            "timestamp": math.floor(mtime_ms),
            "offset": offset,
            "limit": limit,
        },
    )

    data = {
        "type": "text",
        "file": {
            "filePath": file_path,
            "content": content,
            "numLines": line_count,
            "startLine": offset,
            "totalLines": total_lines,
        },
    }

    detect_session_file_type(full_file_path)

    return ToolResult(data=data)


async def _validate_content_tokens(content: str, ext: str, max_tokens: int) -> None:  # noqa: ARG001
    """Validate that file content fits within the token cap.

    Uses a conservative chars/4 estimate; only raises when it clearly exceeds the cap so the
    text-read happy path is unaffected.
    """
    estimate = math.ceil(len(content) / 4)
    if estimate <= max_tokens // 4:
        return
    # Without the API token count we keep the rough estimate as the effective count.
    if estimate > max_tokens:
        raise MaxFileReadTokenExceededError(estimate, max_tokens)


# ---------------------------------------------------------------------------
# read_file_state accessors (works with FileStateCache or a plain dict)
# ---------------------------------------------------------------------------


def _state_get(state: Any, key: str) -> dict[str, Any] | None:
    if state is None:
        return None
    getter = getattr(state, "get", None)
    if getter is not None:
        return getter(key)
    return None


def _state_set(state: Any, key: str, value: dict[str, Any]) -> None:
    if state is None:
        return
    setter = getattr(state, "set", None)
    if setter is not None:
        setter(key, value)
    elif isinstance(state, dict):
        state[key] = value


def _input_get(input: Any, key: str) -> Any:
    """Read ``key`` from a pydantic model or a plain dict input."""
    if isinstance(input, dict):
        return input.get(key)
    return getattr(input, key, None)


# Singleton instance used throughout the tool registry.
file_read_tool = FileReadTool()
