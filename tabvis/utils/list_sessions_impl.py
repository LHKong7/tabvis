"""Standalone implementation of ``listSessions`` for the Agent SDK.

Dependencies are kept minimal and portable — no bootstrap/state, no analytics, no module-scope
mutable state. This module can be imported safely from the SDK entrypoint without triggering CLI
initialization or pulling in expensive dependency chains.

Casing: Python identifiers are snake_case. The returned :class:`SessionInfo` keeps the wire-key
camelCase shape (``sessionId``/``lastModified``/``fileSize``/``customTitle``/``firstPrompt``/
``gitBranch``/``createdAt``) verbatim — it round-trips to the SDK output — so it is a plain
``TypedDict``, not a pydantic model. ``Candidate`` is an internal dataclass (never serialized).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict

from tabvis.utils.get_worktree_paths_portable import get_worktree_paths_portable
from tabvis.utils.session_storage_portable import (
    MAX_SANITIZED_LENGTH,
    LiteSessionFile,
    canonicalize_path,
    extract_first_prompt_from_head,
    extract_json_string_field,
    extract_last_json_string_field,
    find_project_dir,
    get_projects_dir,
    read_session_lite,
    sanitize_path,
    validate_uuid,
)

__all__ = [
    "ListSessionsOptions",
    "SessionInfo",
    "list_candidates",
    "list_sessions_impl",
    "parse_session_info_from_lite",
]


class SessionInfo(TypedDict, total=False):
    """Session metadata returned by :func:`list_sessions_impl`.

    Contains only data extractable from stat + head/tail reads — no full JSONL parsing required.
    Wire keys are preserved (camelCase) since this round-trips to SDK output.
    """

    sessionId: str
    summary: str
    lastModified: int
    fileSize: int
    customTitle: str
    firstPrompt: str
    gitBranch: str
    cwd: str
    tag: str
    # Epoch ms — from first entry's ISO timestamp. Absent if unparseable.
    createdAt: int


class ListSessionsOptions(TypedDict, total=False):
    """Options for :func:`list_sessions_impl`."""

    # Directory to list sessions for. When provided, returns sessions for this project directory
    # (and optionally its git worktrees). When omitted, returns sessions across all projects.
    dir: str
    # Maximum number of sessions to return.
    limit: int
    # Number of sessions to skip from the start of the sorted result set. Defaults to 0.
    offset: int
    # When ``dir`` is provided and inside a git repository, include sessions from all git
    # worktree paths. Defaults to True.
    includeWorktrees: bool


# --------------------------------------------------------------------------------------------
# Field extraction — shared by list_sessions_impl and get_session_info_impl
# --------------------------------------------------------------------------------------------


def parse_session_info_from_lite(
    session_id: str,
    lite: LiteSessionFile,
    project_path: str | None = None,
) -> SessionInfo | None:
    """Parse :class:`SessionInfo` fields from a lite session read (head/tail/stat).

    Returns ``None`` for sidechain sessions or metadata-only sessions with no extractable summary.
    Exported for reuse by ``get_session_info_impl``.
    """
    head = lite["head"]
    tail = lite["tail"]
    mtime = lite["mtime"]
    size = lite["size"]

    # Check first line for sidechain sessions.
    first_newline = head.find("\n")
    first_line = head[:first_newline] if first_newline >= 0 else head
    if '"isSidechain":true' in first_line or '"isSidechain": true' in first_line:
        return None

    # User title (customTitle) wins over AI title (aiTitle); distinct field names mean
    # extract_last_json_string_field naturally disambiguates.
    custom_title = (
        extract_last_json_string_field(tail, "customTitle")
        or extract_last_json_string_field(head, "customTitle")
        or extract_last_json_string_field(tail, "aiTitle")
        or extract_last_json_string_field(head, "aiTitle")
        or None
    )
    first_prompt = extract_first_prompt_from_head(head) or None
    # First entry's ISO timestamp → epoch ms. More reliable than birthtime, which is
    # unsupported on some filesystems.
    first_timestamp = extract_json_string_field(head, "timestamp")
    created_at: int | None = None
    if first_timestamp:
        parsed = _date_parse(first_timestamp)
        if parsed is not None:
            created_at = parsed
    # last-prompt tail entry shows what the user was most recently doing. Head scan is the
    # fallback for sessions without a last-prompt entry.
    summary = (
        custom_title
        or extract_last_json_string_field(tail, "lastPrompt")
        or extract_last_json_string_field(tail, "summary")
        or first_prompt
    )

    # Skip metadata-only sessions (no title, no summary, no prompt).
    if not summary:
        return None
    git_branch = (
        extract_last_json_string_field(tail, "gitBranch")
        or extract_json_string_field(head, "gitBranch")
        or None
    )
    session_cwd = extract_json_string_field(head, "cwd") or project_path or None
    # Type-scope tag extraction to the {"type":"tag"} JSONL line to avoid collision with
    # tool_use inputs containing a `tag` parameter (git tag, Docker tags, cloud resource tags).
    tag_line = _find_last(tail.split("\n"), lambda line: line.startswith('{"type":"tag"'))
    tag = (extract_last_json_string_field(tag_line, "tag") or None) if tag_line is not None else None

    info: SessionInfo = {
        "sessionId": session_id,
        "summary": summary,
        "lastModified": mtime,
        "fileSize": size,
    }
    if custom_title is not None:
        info["customTitle"] = custom_title
    if first_prompt is not None:
        info["firstPrompt"] = first_prompt
    if git_branch is not None:
        info["gitBranch"] = git_branch
    if session_cwd is not None:
        info["cwd"] = session_cwd
    if tag is not None:
        info["tag"] = tag
    if created_at is not None:
        info["createdAt"] = created_at
    return info


def _date_parse(timestamp: str) -> int | None:
    """Epoch ms, or ``None`` when unparseable (NaN)."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def _find_last(items: list[str], predicate) -> str | None:
    """Last element satisfying ``predicate``."""
    for item in reversed(items):
        if predicate(item):
            return item
    return None


# --------------------------------------------------------------------------------------------
# Candidate discovery — stat-only pass. Cheap: 1 syscall per file, no data reads. Lets us
# sort/filter before doing expensive head/tail reads.
# --------------------------------------------------------------------------------------------


@dataclass
class Candidate:
    session_id: str
    file_path: str
    mtime: int
    # Project path for cwd fallback when file lacks a cwd field.
    project_path: str | None = None


async def list_candidates(
    project_dir: str,
    do_stat: bool,
    project_path: str | None = None,
) -> list[Candidate]:
    """List candidate session files in a directory via readdir, optionally stat'ing each for mtime.

    When ``do_stat`` is False, mtime is set to 0 (caller must sort/dedup after reading file
    contents instead).
    """
    try:
        names = await asyncio.to_thread(os.listdir, project_dir)
    except OSError:
        return []

    async def make(name: str) -> Candidate | None:
        if not name.endswith(".jsonl"):
            return None
        session_id = validate_uuid(name[:-6])
        if not session_id:
            return None
        file_path = os.path.join(project_dir, name)
        if not do_stat:
            return Candidate(session_id, file_path, 0, project_path)
        try:
            st = await asyncio.to_thread(os.stat, file_path)
        except OSError:
            return None
        return Candidate(session_id, file_path, int(st.st_mtime * 1000), project_path)

    results = await asyncio.gather(*(make(name) for name in names))
    return [c for c in results if c is not None]


async def _read_candidate(c: Candidate) -> SessionInfo | None:
    """Read a candidate's file contents and extract full :class:`SessionInfo`.

    Returns ``None`` if the session should be filtered out (sidechain, no summary).
    """
    lite = await read_session_lite(c.file_path)
    if not lite:
        return None

    info = parse_session_info_from_lite(c.session_id, lite, c.project_path)
    if not info:
        return None

    # Prefer stat-pass mtime for sort-key consistency; fall back to lite.mtime when
    # do_stat=False (c.mtime is the 0 placeholder).
    if c.mtime:
        info["lastModified"] = c.mtime

    return info


# --------------------------------------------------------------------------------------------
# Sort + limit — batch-read candidates in sorted order until `limit` survivors are collected
# (some candidates filter out on full read).
# --------------------------------------------------------------------------------------------

# Batch size for concurrent reads when walking the sorted candidate list.
READ_BATCH_SIZE = 32


def _compare_desc_key(c: Candidate) -> tuple[int, str]:
    """Sort key for lastModified desc, then sessionId desc (negate via reverse=True)."""
    return (c.mtime, c.session_id)


async def _apply_sort_and_limit(
    candidates: list[Candidate],
    limit: int | None,
    offset: int,
) -> list[SessionInfo]:
    candidates.sort(key=_compare_desc_key, reverse=True)

    sessions: list[SessionInfo] = []
    # limit: 0 means "no limit" (matches get_session_messages semantics).
    want = limit if (limit and limit > 0) else None
    skipped = 0
    # Dedup post-filter: since candidates are sorted mtime-desc, the first non-null read per
    # sessionId is naturally the newest valid copy.
    seen: set[str] = set()

    i = 0
    n = len(candidates)
    while i < n and (want is None or len(sessions) < want):
        batch_end = min(i + READ_BATCH_SIZE, n)
        batch = candidates[i:batch_end]
        results = await asyncio.gather(*(_read_candidate(c) for c in batch))
        for r in results:
            if want is not None and len(sessions) >= want:
                break
            i += 1
            if not r:
                continue
            session_id = r["sessionId"]
            if session_id in seen:
                continue
            seen.add(session_id)
            if skipped < offset:
                skipped += 1
                continue
            sessions.append(r)

    return sessions


async def _read_all_and_sort(candidates: list[Candidate]) -> list[SessionInfo]:
    """Read-all path for when no limit/offset is set.

    Skips the stat pass entirely — reads every candidate, then sorts/dedups on real mtimes from
    read_session_lite. Matches pre-refactor I/O cost (no extra stats).
    """
    all_infos = await asyncio.gather(*(_read_candidate(c) for c in candidates))
    by_id: dict[str, SessionInfo] = {}
    for s in all_infos:
        if not s:
            continue
        existing = by_id.get(s["sessionId"])
        if existing is None or s["lastModified"] > existing["lastModified"]:
            by_id[s["sessionId"]] = s
    sessions = list(by_id.values())
    sessions.sort(key=lambda s: (s["lastModified"], s["sessionId"]), reverse=True)
    return sessions


# --------------------------------------------------------------------------------------------
# Project directory enumeration (single-project vs all-projects)
# --------------------------------------------------------------------------------------------


async def _gather_project_candidates(
    dir_path: str,
    include_worktrees: bool,
    do_stat: bool,
) -> list[Candidate]:
    """Gather candidate session files for a specific project directory (and optionally worktrees)."""
    canonical_dir = await canonicalize_path(dir_path)

    if include_worktrees:
        try:
            worktree_paths = await get_worktree_paths_portable(canonical_dir)
        except Exception:  # noqa: BLE001 - parity with the TS catch-all
            worktree_paths = []
    else:
        worktree_paths = []

    # No worktrees (or git not available / scanning disabled) — scan the single project dir.
    if len(worktree_paths) <= 1:
        project_dir = await find_project_dir(canonical_dir)
        if not project_dir:
            return []
        return await list_candidates(project_dir, do_stat, canonical_dir)

    # Worktree-aware scanning: find all project dirs matching any worktree.
    projects_dir = get_projects_dir()
    case_insensitive = os.name == "nt"

    # Sort worktree paths by sanitized prefix length (longest first) so more specific matches
    # take priority over shorter ones.
    indexed = []
    for wt in worktree_paths:
        sanitized = sanitize_path(wt)
        prefix = sanitized.lower() if case_insensitive else sanitized
        indexed.append((wt, prefix))
    indexed.sort(key=lambda pair: len(pair[1]), reverse=True)

    try:
        all_dirents = await asyncio.to_thread(_scandir, projects_dir)
    except OSError:
        # Fall back to the single project dir.
        project_dir = await find_project_dir(canonical_dir)
        if not project_dir:
            return []
        return await list_candidates(project_dir, do_stat, canonical_dir)

    all_candidates: list[Candidate] = []
    seen_dirs: set[str] = set()

    # Always include the user's actual directory (handles subdirectories like
    # /repo/packages/my-app that won't match worktree root prefixes).
    canonical_project_dir = await find_project_dir(canonical_dir)
    if canonical_project_dir:
        dir_base = os.path.basename(canonical_project_dir)
        seen_dirs.add(dir_base.lower() if case_insensitive else dir_base)
        all_candidates.extend(
            await list_candidates(canonical_project_dir, do_stat, canonical_dir)
        )

    for entry_name, is_dir in all_dirents:
        if not is_dir:
            continue
        dir_name = entry_name.lower() if case_insensitive else entry_name
        if dir_name in seen_dirs:
            continue

        for wt_path, prefix in indexed:
            # Only use startswith for truncated paths (>MAX_SANITIZED_LENGTH) where a hash suffix
            # follows. For short paths, require exact match to avoid /root/project matching
            # /root/project-foo.
            is_match = dir_name == prefix or (
                len(prefix) >= MAX_SANITIZED_LENGTH and dir_name.startswith(prefix + "-")
            )
            if is_match:
                seen_dirs.add(dir_name)
                all_candidates.extend(
                    await list_candidates(
                        os.path.join(projects_dir, entry_name), do_stat, wt_path
                    )
                )
                break

    return all_candidates


def _scandir(path: str) -> list[tuple[str, bool]]:
    """Return ``[(name, is_directory)]`` for ``path`` (readdir withFileTypes parity)."""
    with os.scandir(path) as it:
        return [(entry.name, entry.is_dir()) for entry in it]


async def _gather_all_candidates(do_stat: bool) -> list[Candidate]:
    """Gather candidate session files across all project directories."""
    projects_dir = get_projects_dir()

    try:
        dirents = await asyncio.to_thread(_scandir, projects_dir)
    except OSError:
        return []

    per_project = await asyncio.gather(
        *(
            list_candidates(os.path.join(projects_dir, name), do_stat)
            for name, is_dir in dirents
            if is_dir
        )
    )

    return [c for project in per_project for c in project]


async def list_sessions_impl(
    options: ListSessionsOptions | None = None,
) -> list[SessionInfo]:
    """List sessions with metadata extracted from stat + head/tail reads.

    When ``dir`` is provided, returns sessions for that project directory and its git worktrees.
    When omitted, returns sessions across all projects.

    Pagination via ``limit``/``offset`` operates on the filtered, sorted result set. When either
    is set, a cheap stat-only pass sorts candidates before expensive head/tail reads. When neither
    is set, stat is skipped (read-all-then-sort, same I/O cost as the original implementation).
    """
    opts: ListSessionsOptions = options or {}
    dir_path = opts.get("dir")
    limit = opts.get("limit")
    offset = opts.get("offset")
    include_worktrees = opts.get("includeWorktrees")
    off = offset if offset is not None else 0
    # Only stat when we need to sort before reading (won't read all anyway). limit: 0 means
    # "no limit" (see _apply_sort_and_limit), so treat it as unset.
    do_stat = (limit is not None and limit > 0) or off > 0

    candidates = (
        await _gather_project_candidates(
            dir_path, include_worktrees if include_worktrees is not None else True, do_stat
        )
        if dir_path
        else await _gather_all_candidates(do_stat)
    )

    if not do_stat:
        return await _read_all_and_sort(candidates)
    return await _apply_sort_and_limit(candidates, limit, off)
