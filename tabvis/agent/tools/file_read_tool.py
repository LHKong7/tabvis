"""``Read`` tool — read a file from the local filesystem.

Reads a file from the local filesystem. Supports an optional line ``offset``/``limit`` window
and a ``pages`` parameter for PDFs. Text reads are the primary path and are fully implemented
(dedup against ``read_file_state`` + ``read_file_in_range`` + line-numbered
serialization + cyber-risk reminder + empty/short-file system-reminder). PDF reads prefer
provider-independent text extraction, add page markers and cumulative coverage, and fall back to
native document/image/OCR paths. Image reads use model vision or OCR. Notebook editing has its own
tool; notebook reading remains an explicit unsupported branch in this module.

``max_result_size_chars`` is unbounded — output is bounded by ``maxTokens``
(``validate_content_tokens``); persisting a Read result to a file the model reads back is circular.
``isReadOnly`` / ``isConcurrencySafe`` are both ``True``.

Casing: Python identifiers snake_case; the ``data``/``file`` payload dicts and the
``tool_result`` block keep their wire keys (``filePath``, ``numLines``, ``startLine``,
``totalLines``, ``tool_use_id``, ``media_type``) so they round-trip to the transcript / API.
"""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
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


async def _read_image_data(resolved_file_path: str) -> dict[str, Any]:
    """Read an image file for the active model.

    Multimodal model  -> a native image block (the ``image`` result the mapper already understands).
    Text-only model   -> OCR text if an OCR engine is available (``image_ocr``), else a clear
                         'unavailable' note (``image_unavailable``). Image understanding is gated on
                         vision-OR-OCR: with neither, the capability is simply not offered.
    """
    import base64

    from tabvis.agent.api.providers import resolve_provider_name
    from tabvis.utils.image_resizer import detect_image_format_from_buffer
    from tabvis.utils.model.model import get_main_loop_model, get_model_supports_vision

    def _read_bytes() -> bytes:
        with open(resolved_file_path, "rb") as fh:
            return fh.read()

    raw = await asyncio.to_thread(_read_bytes)
    media_type = detect_image_format_from_buffer(raw)

    model = get_main_loop_model()
    if get_model_supports_vision(model, resolve_provider_name(model)):
        return {
            "type": "image",
            "file": {"base64": base64.b64encode(raw).decode("ascii"), "type": media_type},
        }

    from tabvis.utils import ocr

    if ocr.ocr_enabled() and ocr.ocr_available():
        text = await asyncio.to_thread(ocr.ocr_image_bytes, raw, media_type)
        return {
            "type": "image_ocr",
            "file": {"filePath": resolved_file_path, "text": text or "", "mediaType": media_type},
        }
    ocr.warn_ocr_unavailable_once()
    return {"type": "image_unavailable", "file": {"filePath": resolved_file_path}}


async def _read_pdf_data(resolved_file_path: str, pages: str | None) -> dict[str, Any]:
    """Read a PDF for the active model.

    - Anthropic vision model → the PDF as a native ``document`` block (best fidelity; no poppler).
    - Any other vision model → pages rasterized to images (needs poppler ``pdftoppm``).
    - Text-only model → pages rasterized then OCR'd (needs poppler + an OCR engine).
    - Otherwise → a clear 'unavailable' note (the file is still saved in the workspace).
    """
    import base64 as _b64
    import glob as _glob

    from tabvis.agent.api.providers import resolve_provider_name
    from tabvis.utils.model.model import get_main_loop_model, get_model_supports_vision
    from tabvis.utils.pdf import (
        extract_pdf_pages,
        extract_pdf_text,
        get_pdf_page_count,
        read_pdf,
    )

    model = get_main_loop_model()
    provider = resolve_provider_name(model)
    vision = get_model_supports_vision(model, provider)
    options = parse_pdf_page_range(pages) if pages else None

    page_count = await get_pdf_page_count(resolved_file_path)
    if pages is None and page_count is not None and page_count > PDF_AT_MENTION_INLINE_THRESHOLD:
        return {
            "type": "pdf_unavailable",
            "file": {
                "filePath": resolved_file_path,
                "reason": (
                    f"the PDF has {page_count} pages. Read it in ranges of at most "
                    f"{PDF_MAX_PAGES_PER_READ} pages using the pages parameter, for example "
                    'pages: "1-10". Do not repeat the same unpaged Read request'
                ),
            },
        }

    # Prefer selectable text. It works across model providers and avoids installing a Python PDF
    # package just to inspect a document. Image/native fallbacks remain for scanned PDFs.
    text_result = await extract_pdf_text(resolved_file_path, options)
    if text_result.get("success"):
        text = str(text_result["data"]["file"].get("text") or "")
        if text.strip():
            first_page = int((options or {}).get("firstPage") or 1)
            requested_last = (options or {}).get("lastPage")
            if requested_last and requested_last != math.inf:
                last_page = int(requested_last)
            elif page_count is not None:
                last_page = page_count
            else:
                last_page = first_page + max(0, len(text.rstrip("\f").split("\f")) - 1)
            if page_count is not None:
                last_page = min(last_page, page_count)
            page_chunks = text.rstrip("\f").split("\f")
            marked_text = "\n\n".join(
                f"--- PDF page {first_page + index} ---\n{chunk.strip()}"
                for index, chunk in enumerate(page_chunks)
                if chunk.strip()
            )
            bounded_text = marked_text[:120_000]
            text_truncated = len(marked_text) > len(bounded_text)
            has_more = page_count is not None and last_page < page_count
            next_pages = None
            if has_more:
                next_first = last_page + 1
                next_pages = f"{next_first}-{min(page_count, next_first + PDF_MAX_PAGES_PER_READ - 1)}"
            coverage_complete = (
                page_count is not None
                and first_page == 1
                and last_page >= page_count
                and not text_truncated
            )
            return {
                "type": "pdf_text",
                "file": {
                    "filePath": resolved_file_path,
                    "pages": pages or f"{first_page}-{last_page}",
                    "pageCount": page_count,
                    "firstPage": first_page,
                    "lastPage": last_page,
                    "coverageComplete": coverage_complete,
                    "hasMore": has_more,
                    "nextPages": next_pages,
                    "textTruncated": text_truncated,
                    "text": bounded_text
                    + ("\n…[PDF text truncated; retry with a smaller page range]" if text_truncated else ""),
                },
            }

    # Anthropic vision + native PDF support → send the validated PDF as a document block (no poppler).
    if pages is None and vision and provider == "anthropic" and is_pdf_supported():
        res = await read_pdf(resolved_file_path)
        if res.get("success"):
            return {"type": "pdf_document", "file": res["data"]["file"]}
        err = res.get("error") or {}
        if err.get("reason") in ("empty", "invalid", "too_large", "corrupted", "password_protected"):
            return {"type": "pdf_unavailable", "file": {"filePath": resolved_file_path, "reason": err.get("message")}}
        # else (e.g. transient) fall through to rasterization

    # Rasterize the pages to JPEGs (needs poppler pdftoppm).
    res = await extract_pdf_pages(resolved_file_path, options)
    if res.get("success"):
        out = res["data"]["file"]
        page_paths = sorted(_glob.glob(os.path.join(out["outputDir"], "*.jpg")))
        first_page = int((options or {}).get("firstPage") or 1)
        last_page = first_page + max(0, len(page_paths) - 1)
        if page_count is not None:
            last_page = min(last_page, page_count)
        has_more = page_count is not None and last_page < page_count
        next_pages = None
        if has_more:
            next_first = last_page + 1
            next_pages = f"{next_first}-{min(page_count, next_first + PDF_MAX_PAGES_PER_READ - 1)}"
        page_metadata = {
            "pages": pages or f"{first_page}-{last_page}",
            "pageCount": page_count,
            "firstPage": first_page,
            "lastPage": last_page,
            "coverageComplete": (
                page_count is not None and first_page == 1 and last_page >= page_count
            ),
            "hasMore": has_more,
            "nextPages": next_pages,
            "textTruncated": False,
        }
        if vision:
            images = []
            for p in page_paths:
                with open(p, "rb") as fh:
                    images.append({"data": _b64.b64encode(fh.read()).decode("ascii"), "media_type": "image/jpeg"})
            return {
                "type": "pdf_images",
                "file": {
                    "filePath": resolved_file_path,
                    "count": out["count"],
                    "images": images,
                    **page_metadata,
                },
            }
        from tabvis.utils import ocr

        if ocr.ocr_enabled() and ocr.ocr_available():
            chunks = []
            for i, p in enumerate(page_paths, 1):
                with open(p, "rb") as fh:
                    txt = await asyncio.to_thread(ocr.ocr_image_bytes, fh.read(), "image/jpeg")
                chunks.append(f"--- page {i} ---\n{(txt or '').strip()}")
            return {
                "type": "pdf_ocr",
                "file": {
                    "filePath": resolved_file_path,
                    "count": out["count"],
                    "text": "\n\n".join(chunks),
                    **page_metadata,
                },
            }
        return {
            "type": "pdf_unavailable",
            "file": {
                "filePath": resolved_file_path,
                "reason": "the active model has no vision and no OCR engine is installed "
                "(uv sync --extra ocr, or a tesseract binary)",
            },
        }

    reason = (res.get("error") or {}).get("message", "the PDF could not be processed")
    return {"type": "pdf_unavailable", "file": {"filePath": resolved_file_path, "reason": reason}}


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
            f"files. Maximum {PDF_MAX_PAGES_PER_READ} pages per request. For paper research, "
            "continue through non-overlapping ranges and use the returned nextPages/coverage status; "
            "an abstract page is not a substitute for PDF full text."
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
        "Reading a large PDF without the pages parameter will fail. Maximum 20 pages per request. "
        "PDF text includes explicit page markers, coverage status, and nextPages. In research, an "
        "abstract/landing page is discovery only: read the PDF ranges covering methods, data, "
        "results, limitations, and relevant appendices before calling the paper fully read. Continue "
        "until coverage is sufficient for the user's claims, and cite the page ranges actually read."
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
        if not is_pdf_extension(ext):
            def _has_pdf_magic() -> bool:
                with open(full_file_path, "rb") as fh:
                    return fh.read(5).startswith(b"%PDF-")

            try:
                if await asyncio.to_thread(_has_pdf_magic):
                    # Dynamic endpoints used to be saved as ``get_pdf.cfm``. Content sniffing keeps
                    # those existing workspace files on the native PDF path too.
                    ext = "pdf"
            except OSError:
                pass  # the normal read path below produces the established missing-file error

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

        if data_type == "image_ocr":
            # A non-vision model: the image was OCR'd to text (see _read_image_data).
            file = data["file"]
            text = (file.get("text") or "").strip()
            if text:
                content_str = (
                    f"[Image read via OCR — the active model is not multimodal, so text was "
                    f"extracted from {file['filePath']}]\n\n{text}"
                )
            else:
                content_str = (
                    f"[Image {file['filePath']} read via OCR — no machine-readable text was found. "
                    f"The active model has no vision, so purely visual content cannot be described.]"
                )
            return {"tool_use_id": tool_use_id, "type": "tool_result", "content": content_str}

        if data_type == "image_unavailable":
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": (
                    f"Cannot read image {data['file']['filePath']}: the active model is not "
                    f"multimodal and no OCR engine is installed. Install OCR (`uv sync --extra ocr`, "
                    f"plus a tesseract binary) or switch to a vision-capable model."
                ),
            }

        if data_type == "notebook":
            # a minimal text result referencing the cell count so the block stays well-formed.
            cells = data["file"].get("cells", [])
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Notebook read: {data['file']['filePath']} ({len(cells)} cells)",
            }

        if data_type == "pdf_document":
            # Anthropic-native PDF: hand the model the document block directly.
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": data["file"]["base64"],
                        },
                    }
                ],
            }

        if data_type == "pdf_images":
            file = data["file"]
            blocks: list[dict[str, Any]] = [
                {
                    "type": "text",
                    "text": (
                        f"[PDF {file['filePath']} — pages {file.get('pages') or 'unknown'} "
                        f"of {file.get('pageCount') or 'unknown'} rendered to images]"
                    ),
                }
            ]
            for img in file["images"]:
                blocks.append(
                    {"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}}
                )
            return {"tool_use_id": tool_use_id, "type": "tool_result", "content": blocks}

        if data_type == "pdf_text":
            file = data["file"]
            page_label = f" pages {file['pages']}" if file.get("pages") else ""
            if file.get("coverageComplete"):
                coverage = "Full PDF text coverage is complete for this document."
            elif file.get("hasMore") and file.get("nextPages"):
                coverage = (
                    "This is partial PDF coverage. For research, continue with "
                    f'Read(file_path="{file["filePath"]}", pages="{file["nextPages"]}") and do not '
                    "claim the full paper was read yet."
                )
            else:
                coverage = (
                    "This page range alone does not prove full-document coverage. Confirm the other "
                    "substantive PDF sections before claiming the paper was fully read."
                )
            if file.get("textTruncated"):
                coverage += " The extracted text was truncated; retry this range in smaller chunks."
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": (
                    f"[PDF full-text extraction from {file['filePath']}{page_label}; "
                    f"pageCount={file.get('pageCount') or 'unknown'}]\n\n"
                    f"{(file.get('text') or '').strip()}\n\n"
                    f"<system-reminder>{coverage} Cite only PDF pages actually read. "
                    "The abstract/landing page is not a substitute for methods, results, or "
                    "limitations.</system-reminder>"
                ),
            }

        if data_type == "pdf_ocr":
            file = data["file"]
            body = (file.get("text") or "").strip()
            content_str = (
                f"[PDF {file['filePath']} — pages {file.get('pages') or 'unknown'} of "
                f"{file.get('pageCount') or 'unknown'}, OCR'd because the active model has no "
                f"vision]\n\n{body}"
                if body
                else f"[PDF {file['filePath']} — OCR found no machine-readable text.]"
            )
            if file.get("hasMore") and file.get("nextPages"):
                content_str += (
                    "\n\n<system-reminder>This is partial PDF coverage. Continue with "
                    f'Read(file_path="{file["filePath"]}", pages="{file["nextPages"]}"). '
                    "Do not claim the full paper was read yet.</system-reminder>"
                )
            return {"tool_use_id": tool_use_id, "type": "tool_result", "content": content_str}

        if data_type == "pdf_unavailable":
            file = data["file"]
            return {
                "tool_use_id": tool_use_id,
                "type": "tool_result",
                "content": f"Cannot read PDF {file['filePath']}: {file.get('reason', '')}.",
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
                if file.get("hasMore"):
                    content_str += (
                        "\n\n<system-reminder>"
                        f"Showing lines {file['startLine']}-{file['endLine']} of "
                        f"{file['totalLines']}. Continue with Read offset="
                        f"{file['nextOffset']} limit={file['windowSize']}."
                        "</system-reminder>"
                    )
            elif file["totalLines"] == 0:
                content_str = (
                    "<system-reminder>Warning: the file exists but the contents are empty."
                    "</system-reminder>"
                )
            elif file.get("truncatedByBytes"):
                content_str = (
                    "<system-reminder>The selected line is larger than the bounded Read output "
                    "window, so no complete line could be returned. Use Grep to locate a smaller "
                    "target or Bash to inspect a bounded byte range.</system-reminder>"
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
        return ToolResult(data=await _read_image_data(resolved_file_path))

    # --- PDF ---
    if is_pdf_extension(ext):
        data = await _read_pdf_data(resolved_file_path, pages)
        if data.get("type") in {"pdf_text", "pdf_ocr"}:
            file = data.get("file") or {}
            _update_pdf_coverage(read_file_state, full_file_path, file)
            try:
                from tabvis.browser.artifacts import record_pdf_research_artifact

                await record_pdf_research_artifact(
                    {
                        "file_path": file.get("filePath"),
                        "pages": file.get("pages"),
                        "page_count": file.get("pageCount"),
                        "first_page": file.get("firstPage"),
                        "last_page": file.get("lastPage"),
                        "coverage_complete": file.get("coverageComplete"),
                        "cumulative_page_ranges": file.get("cumulativePageRanges"),
                        "cumulative_text_truncated": file.get("cumulativeTextTruncated"),
                        "next_pages": file.get("nextPages"),
                        "text_truncated": file.get("textTruncated"),
                        "text": file.get("text"),
                    },
                    session_id=context.session_id,
                )
            except Exception:
                pass  # evidence persistence is best-effort and must never break PDF reading
        return ToolResult(data=data)

    # --- Text file (single async read via read_file_in_range) ---
    line_offset = 0 if offset == 0 else offset - 1
    # Progressive disclosure is the default: an omitted limit means the documented 2,000-line
    # window, not an attempt to inject the whole file. Explicit windows behave the same way.
    effective_limit = limit if limit is not None else MAX_LINES_TO_READ
    output_byte_limit = min(max_size_bytes, max_tokens * 4)
    result = await read_file_in_range(
        resolved_file_path,
        line_offset,
        effective_limit,
        output_byte_limit,
        context.abort_controller.signal,
        truncate_on_byte_limit=True,
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

    start_line = line_offset + 1
    end_line = start_line + line_count - 1 if line_count else start_line - 1
    has_more = end_line < total_lines
    data = {
        "type": "text",
        "file": {
            "filePath": file_path,
            "content": content,
            "numLines": line_count,
            "startLine": start_line,
            "endLine": end_line,
            "totalLines": total_lines,
            "hasMore": has_more,
            "nextOffset": end_line + 1 if has_more else None,
            "windowSize": effective_limit,
            "truncatedByBytes": bool(result.get("truncatedByBytes")),
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


def _merge_pdf_page_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for first, last in sorted(ranges):
        if not merged or first > merged[-1][1] + 1:
            merged.append((first, last))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
    return merged


def _update_pdf_coverage(state: Any, key: str, file: dict[str, Any]) -> None:
    """Merge page ranges across Read calls and expose deterministic full-document coverage."""
    first = file.get("firstPage")
    last = file.get("lastPage")
    if not isinstance(first, int) or not isinstance(last, int):
        return
    existing = _state_get(state, key) or {}
    ranges: list[tuple[int, int]] = []
    for item in existing.get("pdfPageRanges") or []:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], int)
        ):
            ranges.append((item[0], item[1]))
    ranges = _merge_pdf_page_ranges([*ranges, (first, last)])

    truncated_ranges: list[tuple[int, int]] = []
    for item in existing.get("pdfTruncatedRanges") or []:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and isinstance(item[0], int)
            and isinstance(item[1], int)
        ):
            truncated_ranges.append((item[0], item[1]))
    if file.get("textTruncated"):
        truncated_ranges = _merge_pdf_page_ranges([*truncated_ranges, (first, last)])
    else:
        truncated_ranges = [
            (start, end)
            for start, end in truncated_ranges
            if not (first <= start and end <= last)
        ]

    page_count = file.get("pageCount")
    next_pages = None
    if isinstance(page_count, int) and page_count > 0:
        next_first = 1
        for start, end in ranges:
            if start > next_first:
                break
            next_first = max(next_first, end + 1)
        if next_first <= page_count:
            next_pages = (
                f"{next_first}-{min(page_count, next_first + PDF_MAX_PAGES_PER_READ - 1)}"
            )
    complete = (
        isinstance(page_count, int)
        and page_count > 0
        and len(ranges) == 1
        and ranges[0][0] == 1
        and ranges[0][1] >= page_count
        and not truncated_ranges
    )
    file["rangeCoverageComplete"] = bool(file.get("coverageComplete"))
    file["coverageComplete"] = complete
    file["cumulativePageRanges"] = [f"{start}-{end}" for start, end in ranges]
    file["cumulativeTextTruncated"] = bool(truncated_ranges)
    file["nextPages"] = next_pages
    file["hasMore"] = next_pages is not None
    _state_set(
        state,
        key,
        {
            **existing,
            "pdfPageRanges": [list(item) for item in ranges],
            "pdfTruncatedRanges": [list(item) for item in truncated_ranges],
        },
    )


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
