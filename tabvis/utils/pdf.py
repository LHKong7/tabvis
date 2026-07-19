"""PDF reading + page extraction

``read_pdf`` validates a PDF (size + magic bytes) and returns it base64-encoded for an Anthropic
``document`` block; ``extract_pdf_pages`` shells out to ``pdftoppm`` (poppler-utils) to rasterize
pages to JPEGs for providers without native PDF support. All entry points return a structured
``PDFResult`` (success/error) — they never raise.
"""

from __future__ import annotations

import os
import re
import uuid as uuid_module
from typing import Generic, Literal, TypedDict, TypeVar

from tabvis.constants.api_limits import PDF_MAX_EXTRACT_SIZE, PDF_TARGET_RAW_SIZE
from tabvis.utils.errors import get_error_message
from tabvis.utils.exec_file_no_throw import exec_file_no_throw
from tabvis.utils.format import format_file_size
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.tool_result_storage import get_tool_results_dir

_T = TypeVar("_T")

PDFErrorReason = Literal[
    "empty",
    "too_large",
    "password_protected",
    "corrupted",
    "unknown",
    "unavailable",
]


class PDFError(TypedDict):
    reason: PDFErrorReason
    message: str


class PDFSuccess(TypedDict, Generic[_T]):
    success: Literal[True]
    data: _T


class PDFFailure(TypedDict):
    success: Literal[False]
    error: PDFError


# PDFResult<T> = PDFSuccess[T] | PDFFailure
PDFResult = dict  # discriminated on the "success" key


class ReadPDFFile(TypedDict):
    filePath: str  # noqa: N815 (TS field key)
    base64: str
    originalSize: int  # noqa: N815


class ReadPDFData(TypedDict):
    type: Literal["pdf"]
    file: ReadPDFFile


async def read_pdf(file_path: str) -> PDFResult:
    """Read a PDF file and return it as base64-encoded data.

    :param file_path: Path to the PDF file.
    :returns: ``PDFResult`` containing PDF data or a structured error.
    """
    try:
        fs = get_fs_implementation()
        stats = await fs.stat(file_path)
        original_size = stats.size

        # Check if file is empty
        if original_size == 0:
            return {
                "success": False,
                "error": {"reason": "empty", "message": f"PDF file is empty: {file_path}"},
            }

        # Check if PDF exceeds maximum size.
        # The API has a 32MB total request limit. After base64 encoding (~33% larger), a PDF must
        # be under ~20MB raw to leave room for conversation context.
        if original_size > PDF_TARGET_RAW_SIZE:
            return {
                "success": False,
                "error": {
                    "reason": "too_large",
                    "message": (
                        f"PDF file exceeds maximum allowed size of "
                        f"{format_file_size(PDF_TARGET_RAW_SIZE)}."
                    ),
                },
            }

        file_buffer = await _read_file_bytes(file_path)

        # Validate PDF magic bytes — reject files that aren't actually PDFs (e.g., HTML renamed to
        # .pdf) before they enter conversation context. An invalid PDF document block poisons the
        # whole session (every subsequent API call 400s) and is unrecoverable without /clear.
        header = file_buffer[:5].decode("ascii", errors="replace")
        if not header.startswith("%PDF-"):
            return {
                "success": False,
                "error": {
                    "reason": "corrupted",
                    "message": f"File is not a valid PDF (missing %PDF- header): {file_path}",
                },
            }

        import base64

        base64_str = base64.b64encode(file_buffer).decode("ascii")

        # Note: We cannot check page count here without parsing the PDF. The API will enforce the
        # 100-page limit and return an error if exceeded.

        return {
            "success": True,
            "data": {
                "type": "pdf",
                "file": {
                    "filePath": file_path,
                    "base64": base64_str,
                    "originalSize": original_size,
                },
            },
        }
    except Exception as e:  # noqa: BLE001 - TS catch(e) maps to an "unknown" error result
        return {
            "success": False,
            "error": {
                "reason": "unknown",
                "message": get_error_message(e),
            },
        }


async def get_pdf_page_count(file_path: str) -> int | None:
    """Get the number of pages in a PDF using ``pdfinfo`` (from poppler-utils).

    Returns ``None`` if ``pdfinfo`` is not available or the page count can't be determined.
    """
    result = await exec_file_no_throw(
        "pdfinfo",
        [file_path],
        {"timeout": 10_000, "use_cwd": False},
    )
    if result.get("code") != 0:
        return None
    match = re.search(r"^Pages:\s+(\d+)", result.get("stdout", ""), flags=re.MULTILINE)
    if not match:
        return None
    try:
        count = int(match.group(1))
    except ValueError:
        return None
    return count


class ExtractPagesFile(TypedDict):
    filePath: str  # noqa: N815
    originalSize: int  # noqa: N815
    count: int
    outputDir: str  # noqa: N815


class PDFExtractPagesResult(TypedDict):
    type: Literal["parts"]
    file: ExtractPagesFile


_pdftoppm_available: bool | None = None


def reset_pdftoppm_cache() -> None:
    """Reset the pdftoppm availability cache. Used by tests only."""
    global _pdftoppm_available
    _pdftoppm_available = None


async def is_pdftoppm_available() -> bool:
    """Check whether the ``pdftoppm`` binary (poppler-utils) is available (cached per process)."""
    global _pdftoppm_available
    if _pdftoppm_available is not None:
        return _pdftoppm_available
    result = await exec_file_no_throw(
        "pdftoppm",
        ["-v"],
        {"timeout": 5000, "use_cwd": False},
    )
    # pdftoppm prints version info to stderr and exits 0 (or sometimes 99 on older versions)
    _pdftoppm_available = result.get("code") == 0 or len(result.get("stderr", "")) > 0
    return _pdftoppm_available


class ExtractPagesOptions(TypedDict, total=False):
    firstPage: int  # noqa: N815 (TS option key)
    lastPage: float  # noqa: N815


async def extract_pdf_pages(
    file_path: str,
    options: ExtractPagesOptions | None = None,
) -> PDFResult:
    """Extract PDF pages as JPEG images using ``pdftoppm``.

    Produces ``page-01.jpg``, ``page-02.jpg``, … in an output directory. This enables reading
    large PDFs and works with all API providers.

    :param file_path: Path to the PDF file.
    :param options: Optional page range (1-indexed, inclusive).
    """
    try:
        fs = get_fs_implementation()
        stats = await fs.stat(file_path)
        original_size = stats.size

        if original_size == 0:
            return {
                "success": False,
                "error": {"reason": "empty", "message": f"PDF file is empty: {file_path}"},
            }

        if original_size > PDF_MAX_EXTRACT_SIZE:
            return {
                "success": False,
                "error": {
                    "reason": "too_large",
                    "message": (
                        f"PDF file exceeds maximum allowed size for text extraction "
                        f"({format_file_size(PDF_MAX_EXTRACT_SIZE)})."
                    ),
                },
            }

        available = await is_pdftoppm_available()
        if not available:
            return {
                "success": False,
                "error": {
                    "reason": "unavailable",
                    "message": (
                        "pdftoppm is not installed. Install poppler-utils (e.g. "
                        "`brew install poppler` or `apt-get install poppler-utils`) to enable "
                        "PDF page rendering."
                    ),
                },
            }

        uuid = str(uuid_module.uuid4())
        output_dir = os.path.join(get_tool_results_dir(), f"pdf-{uuid}")
        os.makedirs(output_dir, exist_ok=True)

        # pdftoppm produces files like <prefix>-01.jpg, <prefix>-02.jpg, etc.
        prefix = os.path.join(output_dir, "page")
        args = ["-jpeg", "-r", "100"]
        opts = options or {}
        if opts.get("firstPage"):
            args.extend(["-f", str(opts["firstPage"])])
        last_page = opts.get("lastPage")
        if last_page and last_page != float("inf"):
            args.extend(["-l", str(int(last_page))])
        args.extend([file_path, prefix])
        result = await exec_file_no_throw(
            "pdftoppm",
            args,
            {"timeout": 120_000, "use_cwd": False},
        )

        code = result.get("code")
        stderr = result.get("stderr", "")
        if code != 0:
            if re.search(r"password", stderr, flags=re.IGNORECASE):
                return {
                    "success": False,
                    "error": {
                        "reason": "password_protected",
                        "message": (
                            "PDF is password-protected. Please provide an unprotected version."
                        ),
                    },
                }
            if re.search(r"damaged|corrupt|invalid", stderr, flags=re.IGNORECASE):
                return {
                    "success": False,
                    "error": {
                        "reason": "corrupted",
                        "message": "PDF file is corrupted or invalid.",
                    },
                }
            return {
                "success": False,
                "error": {"reason": "unknown", "message": f"pdftoppm failed: {stderr}"},
            }

        # Read generated image files and sort naturally
        entries = os.listdir(output_dir)
        image_files = sorted(f for f in entries if f.endswith(".jpg"))
        page_count = len(image_files)

        if page_count == 0:
            return {
                "success": False,
                "error": {
                    "reason": "corrupted",
                    "message": "pdftoppm produced no output pages. The PDF may be invalid.",
                },
            }

        count = len(image_files)

        return {
            "success": True,
            "data": {
                "type": "parts",
                "file": {
                    "filePath": file_path,
                    "originalSize": original_size,
                    "outputDir": output_dir,
                    "count": count,
                },
            },
        }
    except Exception as e:  # noqa: BLE001 - TS catch(e) maps to an "unknown" error result
        return {
            "success": False,
            "error": {
                "reason": "unknown",
                "message": get_error_message(e),
            },
        }


async def _read_file_bytes(file_path: str) -> bytes:
    """``fs/promises.readFile`` (raw bytes) analogue, off-thread."""
    import asyncio

    return await asyncio.to_thread(lambda: open(file_path, "rb").read())  # noqa: SIM115
