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
from urllib.parse import unquote, urlparse

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
