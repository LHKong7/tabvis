"""The download **workspace** — where browser downloads and fetched files (web PDFs, links) land so
the agent can then read/evaluate them.

Default location is **per run**: ``<config-home>/projects/<sanitized-cwd>/<session-id>/workspace/``
(beside that session's ``browser-artifacts/``). Override with ``TABVIS_WORKSPACE_DIR`` (an absolute
path used verbatim — NOT per-session), e.g. to point every run at one folder you watch.

Files are written with collision-free names (``report.pdf`` → ``report (2).pdf`` …) so nothing is
ever clobbered. Names are sanitized (basename only, unsafe chars → ``_``) so a hostile
``suggested_filename`` can't escape the workspace.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, unquote_to_bytes, urlparse

_UNSAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def get_workspace_dir(*, create: bool = False) -> str:
    """Absolute path to the download workspace. ``create=True`` makes the directory."""
    override = (os.environ.get("TABVIS_WORKSPACE_DIR") or "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    else:
        # Per-session, beside browser-artifacts (see tabvis/browser/session.py:get_session_dir).
        from tabvis.browser.session import get_session_dir

        path = os.path.join(get_session_dir(), "workspace")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def get_quarantine_dir(*, create: bool = False) -> str:
    """Absolute path to the download **quarantine** — where an *unexpected* download that policy did
    not clear is held, out of the agent's reach (issue #3).

    Sits beside the workspace but is deliberately NOT the workspace: the agent's Read tool targets
    the workspace, so a quarantined file is recorded (as an artifact) and kept for a human to
    inspect/approve/delete, without being handed to the model. Per-session, like the workspace.
    """
    override = (os.environ.get("TABVIS_WORKSPACE_DIR") or "").strip()
    if override:
        path = os.path.join(os.path.abspath(os.path.expanduser(override)), "_quarantine")
    else:
        from tabvis.browser.session import get_session_dir

        path = os.path.join(get_session_dir(), "quarantine")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _safe_name(name: str | None, fallback: str = "download") -> str:
    """basename-only, unsafe chars → '_', trimmed — can never contain a path separator."""
    raw = (name or "").replace("\\", "/")
    base = os.path.basename(raw).strip()
    cleaned = _UNSAFE.sub("_", base).strip(". ")
    return cleaned[:200] or fallback


def filename_from_url(url: str | None, default: str = "download") -> str:
    """A sane download filename derived from a URL's path (percent-decoded, sanitized)."""
    try:
        name = unquote(os.path.basename(urlparse(url or "").path))
    except (ValueError, TypeError):
        name = ""
    return _safe_name(name or default, default)


def filename_from_response(
    url: str | None,
    headers: dict[str, str] | None,
    default: str = "download",
) -> str:
    """Derive a safe filename from response metadata, falling back to the URL.

    ``Content-Disposition`` wins because dynamic download endpoints often end in ``.cfm``/``.php``
    while serving a named PDF. If the response says it is a PDF and neither source supplies a PDF
    suffix, append ``.pdf`` so the Read tool takes its native PDF path instead of treating the bytes
    as an oversized text file.
    """
    normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    disposition = normalized.get("content-disposition", "")
    name = _filename_from_disposition(disposition)
    filename = _safe_name(name, default) if name else filename_from_url(url, default)
    if "application/pdf" in normalized.get("content-type", "").lower():
        filename = ensure_pdf_filename(filename)
    return filename


def ensure_pdf_filename(filename: str | None, default: str = "page.pdf") -> str:
    """Return a safe filename whose final suffix is ``.pdf``."""
    safe = _safe_name(filename, default)
    return safe if safe.lower().endswith(".pdf") else f"{safe}.pdf"


def _filename_from_disposition(value: str) -> str | None:
    """Small RFC 5987/Content-Disposition filename parser (basename sanitizing happens later)."""
    if not value:
        return None
    extended = re.search(r"(?:^|;)\s*filename\*\s*=\s*([^;]+)", value, re.IGNORECASE)
    if extended:
        raw = extended.group(1).strip().strip('"')
        if "''" in raw:
            charset, encoded = raw.split("''", 1)
            try:
                return unquote_to_bytes(encoded).decode(charset or "utf-8", errors="replace")
            except (LookupError, UnicodeDecodeError):
                return unquote(encoded)
        return unquote(raw)
    plain = re.search(r'(?:^|;)\s*filename\s*=\s*(?:"([^"]*)"|([^;]+))', value, re.IGNORECASE)
    if plain:
        return (plain.group(1) or plain.group(2) or "").strip()
    return None


def unique_path(dir_path: str, filename: str | None) -> str:
    """A collision-free absolute path under ``dir_path`` (creates the dir).

    ``report.pdf`` → ``report.pdf``, then ``report (2).pdf``, ``report (3).pdf``, …
    """
    os.makedirs(dir_path, exist_ok=True)
    base = _safe_name(filename)
    stem, ext = os.path.splitext(base)
    candidate = os.path.join(dir_path, base)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dir_path, f"{stem} ({counter}){ext}")
        counter += 1
    return candidate
