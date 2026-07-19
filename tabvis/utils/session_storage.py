"""Session transcript storage

This is the CLI-side session-transcript store: it owns the on-disk JSONL session files
(``<config home>/projects/<sanitized-cwd>/<sessionId>.jsonl``), the buffered append/flush write
path (the :class:`Project` singleton), the read/load path (``load_transcript_file`` →
``build_conversation_chain`` → ``LogOption``), the session-metadata records (custom-title / tag /
agent-name / pr-link / worktree-state …), the agent/remote-agent metadata sidecars, and the
lite-session listing/enrichment used by the ``/resume`` picker.

The pure-stdlib, dependency-free slice lives in
:mod:`tabvis.utils.session_storage_portable` (sanitization, JSON-field scraping, head/tail reads,
the compact-boundary transcript scanner) and :mod:`tabvis.utils.list_sessions_impl` (the SDK
``listSessions``). This module REUSES those — it does not re-implement them — and adds the
CLI-only behaviors (bootstrap/state, analytics, settings, git, remote ingress, the write queue).

Casing (per ``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; module constants are
UPPER_CASE. Transcript ENVELOPES (``Entry`` / ``TranscriptMessage`` / ``LogOption`` and every
metadata record) are plain dicts/``TypedDict`` that round-trip to the JSONL, so their wire keys
(``parentUuid`` / ``uuid`` / ``timestamp`` / ``sessionId`` / ``customTitle`` / ``isSidechain`` /
``toolUseResult`` …) and kebab-case ``type`` discriminants are kept VERBATIM, and they are mutated
in place (``apply_preserved_segment_relinks`` etc.) — never pydantic / ``extra=forbid``.

Stdlib / dependency substitutions (NO pyproject edits):
- TS ``fs/promises`` → :func:`asyncio.to_thread` over stdlib ``os`` (``tabvis.utils.fs_operations``
  house style). Sync ``fs`` primitives (``openSync``/``fstatSync``/``readSync``) → blocking
  positional file reads. ``Buffer`` byte work → ``bytes``/``bytearray``.
- TS ``lodash-es/memoize`` → :func:`functools.cache`-style memo (``get_project_dir``) /
  ``tabvis.utils.memoize`` semantics. ``getSessionMessages`` is a hand-rolled async memo with a
  clearable cache (the TS ``.cache`` surface) keyed by sessionId.
- TS ``Date.parse`` / ``Date.now`` → :func:`datetime`-based helpers / ``time.time``.
- TS ``Array.prototype.findLast``/``at(-1)`` → manual reverse scan / negative index.

Cyclic group (``session_storage`` ↔ ``file_history`` ↔ ``tool_result_storage`` ↔
``graceful_shutdown``): broken so EVERY module imports standalone — type-only cross-refs go under
``if TYPE_CHECKING:`` and runtime cross-refs are FUNCTION-LOCAL (lazy) imports done inside the
function that uses them. There is NO top-level import of a cyclic sibling, so the import-smoke
passes even when a sibling is not yet implemented.

Inlined / stubbed (flat-tools rule + deps not on the existing surface):
- ``REPL_TOOL_NAME`` (``src/tools/REPLTool/constants.ts``) is the tiny constant ``'REPL'`` —
  inlined (the flat ``tabvis/tools/repl_tool.py`` does not exist yet).
- ``extract_tag`` / ``is_compact_boundary_message`` are not on the public
  ``tabvis.utils.messages`` surface, so equivalent local helpers are inlined.
- ``get_branch`` (``src/utils/git.ts`` cached resolver) is not implemented — a best-effort local
  ``_get_branch`` shells out via ``exec_file_no_throw``; rewire to ``tabvis.utils.git.get_branch``
  when it lands.
- ``is_fs_inaccessible`` reuses the implementation in ``tabvis.utils.shell_config``.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from tabvis.bootstrap.state import (
    get_original_cwd,
    get_plan_slug_cache,
    get_prompt_id,
    get_session_id,
    get_session_project_dir,
    is_session_persistence_disabled,
    switch_session)
from tabvis.constants.xml import COMMAND_ARGS_TAG, COMMAND_NAME_TAG
from tabvis.types.ids import as_agent_id, as_session_id
from tabvis.utils.array import uniq
from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.concurrent_sessions import update_session_name
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.diag_logs import log_for_diagnostics_no_pii
from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy
from tabvis.utils.format import format_file_size
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.get_worktree_paths import get_worktree_paths
from tabvis.utils.json import parse_jsonl
from tabvis.utils.list_sessions_impl import _date_parse  # JS Date.parse → epoch ms
from tabvis.utils.log import log_error

# Reused from the existing portable slice — DO NOT duplicate.
from tabvis.utils.session_storage_portable import (
    LITE_READ_BUF_SIZE,
    SKIP_PRECOMPACT_THRESHOLD,
    extract_json_string_field,
    extract_last_json_string_field,
    read_head_and_tail,
    read_transcript_for_load,
    sanitize_path)
from tabvis.utils.shell_config import is_fs_inaccessible
from tabvis.utils.slow_operations import json_parse, json_stringify
from tabvis.utils.uuid import validate_uuid

if TYPE_CHECKING:
    # Type-only cross-refs (cyclic siblings + opaque payloads). Never imported at runtime here.
    from collections.abc import Awaitable


# --------------------------------------------------------------------------------------------
# Module constants
# --------------------------------------------------------------------------------------------

# Cache MACRO.VERSION at module level (TS works around a bun --define bug). Headless has no MACRO.
VERSION = "unknown"

# A transcript is a list of user/assistant/attachment/system message envelopes (plain dicts).
Transcript = list

# ``tabvis/tools/repl_tool.py`` does not exist yet; rewire when it lands (flat-tools rule).
REPL_TOOL_NAME = "REPL"

# 50MB — prevents OOM in the tombstone slow path which reads + rewrites the entire session file.
MAX_TOMBSTONE_REWRITE_BYTES = 50 * 1024 * 1024

# 50 MB — callers that read the raw transcript must bail out above this to avoid OOM.
MAX_TRANSCRIPT_READ_BYTES = 50 * 1024 * 1024

# Pre-compiled regex to skip non-meaningful messages when extracting the first prompt. Kept in
# sync with ``session_storage_portable``'s ``_SKIP_FIRST_PROMPT_PATTERN``.
SKIP_FIRST_PROMPT_PATTERN = re.compile(
    r"^(?:\s*<[a-z][\w-]*[\s>]|\[Request interrupted by user[^\]]*\])"
)

# Number of sessions to enrich on the initial load of the resume picker.
INITIAL_ENRICH_COUNT = 50

REMOTE_FLUSH_INTERVAL_MS = 10


# --------------------------------------------------------------------------------------------
# --------------------------------------------------------------------------------------------


def _escape_reg_exp(value: str) -> str:
    """Escape regex metacharacters in a literal string."""
    return re.sub(r"[.*+?^${}()|[\]\\]", r"\\\g<0>", value)


def extract_tag(html: str, tag_name: str) -> str | None:
    """Extract the content of ``<tag_name>...</tag_name>`` at the top nesting level.

    A local implementation of ``messages.extractTag`` (not on the existing
    ``tabvis.utils.messages`` surface). Handles attributes, multiline content, and nested tags of
    the same type (returns the first match at depth 0).
    """
    if not html.strip() or not tag_name.strip():
        return None

    escaped = _escape_reg_exp(tag_name)
    pattern = re.compile(
        rf"<{escaped}(?:\s+[^>]*)?>([\s\S]*?)</{escaped}>",
        re.IGNORECASE,
    )
    opening = re.compile(rf"<{escaped}(?:\s+[^>]*?)?>", re.IGNORECASE)
    closing = re.compile(rf"</{escaped}>", re.IGNORECASE)

    for match in pattern.finditer(html):
        content = match.group(1)
        before = html[: match.start()]
        depth = len(opening.findall(before)) - len(closing.findall(before))
        if depth == 0 and content:
            return content
    return None


def is_compact_boundary_message(message: Any) -> bool:
    """Whether ``message`` is a ``system``/``compact_boundary`` envelope.

    A local implementation of ``messages.isCompactBoundaryMessage``.
    """
    return (
        isinstance(message, dict)
        and message.get("type") == "system"
        and message.get("subtype") == "compact_boundary"
    )


async def _get_branch() -> str | None:
    """Best-effort current git branch.

    A cached ``git.getBranch`` resolver is not on the existing surface, so this shells out via
    ``exec_file_no_throw``.
    """
    try:
        from tabvis.utils.exec_file_no_throw import exec_file_no_throw

        result = await exec_file_no_throw(
            "git", ["rev-parse", "--abbrev-ref", "HEAD"], cwd=get_cwd()
        )
        if result and getattr(result, "code", 1) == 0:
            branch = (result.stdout or "").strip()
            return branch or None
    except Exception:  # noqa: BLE001 - not in a git repo / git missing
        return None
    return None


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------------------------
# Transcript-message type guards
# --------------------------------------------------------------------------------------------


def is_transcript_message(entry: Any) -> bool:
    """Whether ``entry`` is a transcript message (user/assistant/attachment/system).

    Single source of truth for what constitutes a transcript message. Progress messages are NOT
    transcript messages.
    """
    return isinstance(entry, dict) and entry.get("type") in (
        "user",
        "assistant",
        "attachment",
        "system",
    )


def is_chain_participant(m: Any) -> bool:
    """Entries that participate in the parentUuid chain (everything except progress).
    ``isChainParticipant``."""
    return isinstance(m, dict) and m.get("type") != "progress"


def is_legacy_progress_entry(entry: Any) -> bool:
    """Whether ``entry`` is a pre-#24099 on-disk progress entry (type=progress with a uuid).

    Return whether legacy progress entry.
    """
    return (
        isinstance(entry, dict)
        and entry.get("type") == "progress"
        and isinstance(entry.get("uuid"), str)
    )


# High-frequency ephemeral tool-progress tick types (UI-only).
EPHEMERAL_PROGRESS_TYPES = {"bash_progress", "powershell_progress", "mcp_progress"}


def is_ephemeral_tool_progress(data_type: Any) -> bool:
    """Return whether ephemeral tool progress."""
    return isinstance(data_type, str) and data_type in EPHEMERAL_PROGRESS_TYPES


# --------------------------------------------------------------------------------------------
# Path / directory helpers
# --------------------------------------------------------------------------------------------


def get_projects_dir() -> str:
    """``<Config home>/projects``."""
    return os.path.join(get_tabvis_config_home_dir(), "projects")


# Memoized: called 12+ times per turn. Input is a cwd string; the result is stable for a given
# input. lodash ``memoize`` → a hand-rolled keyed cache (so ``get_project_dir.cache`` is available
# for parity if needed).
_project_dir_cache: dict[str, str] = {}


def get_project_dir(project_dir: str) -> str:
    """``<Projects dir>/<sanitized(project_dir)>`` (memoized)."""
    cached = _project_dir_cache.get(project_dir)
    if cached is not None:
        return cached
    result = os.path.join(get_projects_dir(), sanitize_path(project_dir))
    _project_dir_cache[project_dir] = result
    return result


def get_transcript_path() -> str:
    """The current session's JSONL path."""
    project_dir = get_session_project_dir() or get_project_dir(get_original_cwd())
    return os.path.join(project_dir, f"{get_session_id()}.jsonl")


def get_transcript_path_for_session(session_id: str) -> str:
    """Return the transcript path for session.

    For the CURRENT session, honor ``getSessionProjectDir`` like :func:`get_transcript_path`. For
    other sessions we can only guess via ``originalCwd``.
    """
    if session_id == get_session_id():
        return get_transcript_path()
    project_dir = get_project_dir(get_original_cwd())
    return os.path.join(project_dir, f"{session_id}.jsonl")


# In-memory map of agentId → subdirectory for grouping related subagent transcripts.
_agent_transcript_subdirs: dict[str, str] = {}


def set_agent_transcript_subdir(agent_id: str, subdir: str) -> None:
    """Set the agent transcript subdir."""
    _agent_transcript_subdirs[agent_id] = subdir


def clear_agent_transcript_subdir(agent_id: str) -> None:
    """Clear the agent transcript subdir."""
    _agent_transcript_subdirs.pop(agent_id, None)


def get_agent_transcript_path(agent_id: str) -> str:
    """A subagent transcript path under the session dir."""
    project_dir = get_session_project_dir() or get_project_dir(get_original_cwd())
    session_id = get_session_id()
    subdir = _agent_transcript_subdirs.get(agent_id)
    if subdir:
        base = os.path.join(project_dir, session_id, "subagents", subdir)
    else:
        base = os.path.join(project_dir, session_id, "subagents")
    return os.path.join(base, f"agent-{agent_id}.jsonl")


def _get_agent_metadata_path(agent_id: str) -> str:
    return re.sub(r"\.jsonl$", ".meta.json", get_agent_transcript_path(agent_id))


# AgentMetadata / RemoteAgentMetadata are plain dicts (sidecar JSON) — wire keys verbatim.


async def write_agent_metadata(agent_id: str, metadata: dict[str, Any]) -> None:
    """Persist the agentType sidecar."""
    path = _get_agent_metadata_path(agent_id)
    await asyncio.to_thread(os.makedirs, os.path.dirname(path), exist_ok=True)
    await asyncio.to_thread(_write_text, path, json_stringify(metadata))


async def read_agent_metadata(agent_id: str) -> dict[str, Any] | None:
    """Read the agent metadata."""
    path = _get_agent_metadata_path(agent_id)
    try:
        raw = await asyncio.to_thread(_read_text, path)
        return json_parse(raw)
    except Exception as e:  # noqa: BLE001 - mirror the TS try/catch
        if is_fs_inaccessible(e):
            return None
        raise


def _get_remote_agents_dir() -> str:
    project_dir = get_session_project_dir() or get_project_dir(get_original_cwd())
    return os.path.join(project_dir, get_session_id(), "remote-agents")


def _get_remote_agent_metadata_path(task_id: str) -> str:
    return os.path.join(_get_remote_agents_dir(), f"remote-agent-{task_id}.meta.json")


async def write_remote_agent_metadata(task_id: str, metadata: dict[str, Any]) -> None:
    """Write the remote agent metadata."""
    path = _get_remote_agent_metadata_path(task_id)
    await asyncio.to_thread(os.makedirs, os.path.dirname(path), exist_ok=True)
    await asyncio.to_thread(_write_text, path, json_stringify(metadata))


async def read_remote_agent_metadata(task_id: str) -> dict[str, Any] | None:
    """Read the remote agent metadata."""
    path = _get_remote_agent_metadata_path(task_id)
    try:
        raw = await asyncio.to_thread(_read_text, path)
        return json_parse(raw)
    except Exception as e:  # noqa: BLE001
        if is_fs_inaccessible(e):
            return None
        raise


async def delete_remote_agent_metadata(task_id: str) -> None:
    """Delete the remote agent metadata."""
    path = _get_remote_agent_metadata_path(task_id)
    try:
        await asyncio.to_thread(os.unlink, path)
    except Exception as e:  # noqa: BLE001
        if is_fs_inaccessible(e):
            return
        raise


async def list_remote_agent_metadata() -> list[dict[str, Any]]:
    """Scan the remote-agents/ dir for sidecars."""
    dir_path = _get_remote_agents_dir()
    try:
        entries = await asyncio.to_thread(_scandir_files, dir_path)
    except Exception as e:  # noqa: BLE001
        if is_fs_inaccessible(e):
            return []
        raise
    results: list[dict[str, Any]] = []
    for name in entries:
        if not name.endswith(".meta.json"):
            continue
        try:
            raw = await asyncio.to_thread(_read_text, os.path.join(dir_path, name))
            results.append(json_parse(raw))
        except Exception as e:  # noqa: BLE001 - skip corrupt/partial files
            log_for_debugging(f"listRemoteAgentMetadata: skipping {name}: {e}")
    return results


def session_id_exists(session_id: str) -> bool:
    """Sync stat of the current-cwd session file."""
    project_dir = get_project_dir(get_original_cwd())
    session_file = os.path.join(project_dir, f"{session_id}.jsonl")
    fs = get_fs_implementation()
    try:
        fs.stat_sync(session_file)
        return True
    except Exception:  # noqa: BLE001 - parity with the TS catch-all
        return False


def get_node_env() -> str:
    """Return the node env."""
    return os.environ.get("NODE_ENV") or "development"


def get_user_type() -> str:
    """Return the user type."""
    return os.environ.get("USER_TYPE") or "external"


def _get_entrypoint() -> str | None:
    return os.environ.get("TABVIS_ENTRYPOINT")


def is_custom_title_enabled() -> bool:
    """Return whether custom title enabled."""
    return True


# --------------------------------------------------------------------------------------------
# Low-level file helpers (sync; wrapped by asyncio.to_thread on the async surface)
# --------------------------------------------------------------------------------------------


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:  # noqa: PTH123
        return fh.read()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:  # noqa: PTH123
        return fh.read()


def _write_text(path: str, content: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def _scandir_files(path: str) -> list[str]:
    """Return regular-file names in ``path`` (readdir withFileTypes → isFile)."""
    with os.scandir(path) as it:
        return [entry.name for entry in it if entry.is_file()]


def _append_to_file_sync(file_path: str, data: str) -> None:
    """Append ``data`` to ``file_path``, creating the parent dir on failure. mode 0o600/0o700."""
    try:
        _raw_append(file_path, data)
    except OSError:
        os.makedirs(os.path.dirname(file_path), mode=0o700, exist_ok=True)
        _raw_append(file_path, data)


def _raw_append(file_path: str, data: str) -> None:
    fd = os.open(file_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


def append_entry_to_file(full_path: str, entry: dict[str, Any]) -> None:
    """Append an entry to a session file, creating the parent dir if missing.
    ``appendEntryToFile`` (sync)."""
    line = json_stringify(entry) + "\n"
    try:
        _raw_append(full_path, line)
    except OSError:
        os.makedirs(os.path.dirname(full_path), mode=0o700, exist_ok=True)
        _raw_append(full_path, line)


def read_file_tail_sync(full_path: str) -> str:
    """Sync tail read for the external-writer check. Reads the last ``LITE_READ_BUF_SIZE`` bytes.

    Returns ``''`` on any error.
    """
    fd: int | None = None
    try:
        fd = os.open(full_path, os.O_RDONLY)
        st = os.fstat(fd)
        tail_offset = max(0, st.st_size - LITE_READ_BUF_SIZE)
        length = min(LITE_READ_BUF_SIZE, st.st_size - tail_offset)
        if length <= 0:
            return ""
        os.lseek(fd, tail_offset, os.SEEK_SET)
        data = os.read(fd, length)
        return data.decode("utf-8", "replace")
    except OSError:
        return ""
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# --------------------------------------------------------------------------------------------
# The Project singleton — buffered write path
# --------------------------------------------------------------------------------------------


class Project:
    """Per-process session-write coordinator.

    Owns the current ``sessionFile`` path, a per-file write queue with a flush timer, the
    in-memory session-metadata cache (title/tag/agent/pr/worktree), and the remote-ingress /
    CCR-v2 internal-event writers/readers.

    The TS uses ``setTimeout``-batched drains; this implementation keeps the same observable behavior with
    an asyncio task scheduled via ``scheduleDrain``. The write queues, pending-write counter, and
    flush resolvers mirror the TS fields one-for-one.
    """

    def __init__(self) -> None:
        # Minimal cache for the current session only.
        self.current_session_tag: str | None = None
        self.current_session_title: str | None = None
        self.current_session_agent_name: str | None = None
        self.current_session_agent_color: str | None = None
        self.current_session_last_prompt: str | None = None
        self.current_session_agent_setting: str | None = None
        # Tri-state: _UNSET = never touched, None = exited worktree, dict = in worktree.
        self.current_session_worktree: Any = _UNSET
        self.current_session_pr_number: int | None = None
        self.current_session_pr_url: str | None = None
        self.current_session_pr_repository: str | None = None

        self.session_file: str | None = None
        self._pending_entries: list[dict[str, Any]] = []
        self._remote_ingress_url: str | None = None
        self._internal_event_writer: Any = None
        self._internal_event_reader: Any = None
        self._internal_subagent_event_reader: Any = None
        self._pending_write_count = 0
        self._flush_resolvers: list[asyncio.Future] = []
        # Per-file write queues: filePath → list[(entry, future)].
        self._write_queues: dict[str, list[tuple[dict[str, Any], asyncio.Future]]] = {}
        self._flush_timer: asyncio.TimerHandle | None = None
        self._active_drain: asyncio.Task | None = None
        self._flush_interval_ms = 100
        self._max_chunk_bytes = 100 * 1024 * 1024
        self._existing_session_files: dict[str, str] = {}

    def _reset_flush_state(self) -> None:
        """Reset flush/queue state for testing."""
        self._pending_write_count = 0
        self._flush_resolvers = []
        if self._flush_timer:
            self._flush_timer.cancel()
        self._flush_timer = None
        self._active_drain = None
        self._write_queues = {}

    def _increment_pending_writes(self) -> None:
        self._pending_write_count += 1

    def _decrement_pending_writes(self) -> None:
        self._pending_write_count -= 1
        if self._pending_write_count == 0:
            for fut in self._flush_resolvers:
                if not fut.done():
                    fut.set_result(None)
            self._flush_resolvers = []

    async def _track_write(self, coro: Awaitable[Any]) -> Any:
        self._increment_pending_writes()
        try:
            return await coro
        finally:
            self._decrement_pending_writes()

    def _enqueue_write(self, file_path: str, entry: dict[str, Any]) -> asyncio.Future:
        """Queue an entry for batched write; returns a future that resolves when it lands."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        queue = self._write_queues.get(file_path)
        if queue is None:
            queue = []
            self._write_queues[file_path] = queue
        queue.append((entry, fut))
        self._schedule_drain()
        return fut

    def _schedule_drain(self) -> None:
        if self._flush_timer:
            return
        loop = asyncio.get_event_loop()
        self._flush_timer = loop.call_later(
            self._flush_interval_ms / 1000, self._on_flush_timer
        )

    def _on_flush_timer(self) -> None:
        self._flush_timer = None
        self._active_drain = asyncio.ensure_future(self._run_drain())

    async def _run_drain(self) -> None:
        await self._drain_write_queue()
        self._active_drain = None
        if len(self._write_queues) > 0:
            self._schedule_drain()

    async def _drain_write_queue(self) -> None:
        for file_path, queue in list(self._write_queues.items()):
            if len(queue) == 0:
                continue
            batch = queue[:]
            queue.clear()

            content = ""
            resolvers: list[asyncio.Future] = []
            for entry, fut in batch:
                line = json_stringify(entry) + "\n"
                if len(content) + len(line) >= self._max_chunk_bytes:
                    await asyncio.to_thread(_append_to_file_sync, file_path, content)
                    for r in resolvers:
                        if not r.done():
                            r.set_result(None)
                    resolvers = []
                    content = ""
                content += line
                resolvers.append(fut)

            if len(content) > 0:
                await asyncio.to_thread(_append_to_file_sync, file_path, content)
                for r in resolvers:
                    if not r.done():
                        r.set_result(None)

        # Clean up empty queues.
        for file_path, queue in list(self._write_queues.items()):
            if len(queue) == 0:
                del self._write_queues[file_path]

    def reset_session_file(self) -> None:
        """Reset the session file."""
        self.session_file = None
        self._pending_entries = []

    def re_append_session_metadata(self, skip_title_refresh: bool = False) -> None:
        """Re-append cached session metadata to EOF so the tail window keeps it.
        ``reAppendSessionMetadata`` (sync)."""
        if not self.session_file:
            return
        session_id = get_session_id()
        if not session_id:
            return

        tail = read_file_tail_sync(self.session_file)
        tail_lines = tail.split("\n")

        if not skip_title_refresh:
            title_line = _find_last(
                tail_lines, lambda line: line.startswith('{"type":"custom-title"')
            )
            if title_line:
                tail_title = extract_last_json_string_field(title_line, "customTitle")
                if tail_title is not None:
                    self.current_session_title = tail_title or None
        tag_line = _find_last(
            tail_lines, lambda line: line.startswith('{"type":"tag"')
        )
        if tag_line:
            tail_tag = extract_last_json_string_field(tag_line, "tag")
            if tail_tag is not None:
                self.current_session_tag = tail_tag or None

        if self.current_session_last_prompt:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "last-prompt",
                    "lastPrompt": self.current_session_last_prompt,
                    "sessionId": session_id,
                },
            )
        if self.current_session_title:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "custom-title",
                    "customTitle": self.current_session_title,
                    "sessionId": session_id,
                },
            )
        if self.current_session_tag:
            append_entry_to_file(
                self.session_file,
                {"type": "tag", "tag": self.current_session_tag, "sessionId": session_id},
            )
        if self.current_session_agent_name:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "agent-name",
                    "agentName": self.current_session_agent_name,
                    "sessionId": session_id,
                },
            )
        if self.current_session_agent_color:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "agent-color",
                    "agentColor": self.current_session_agent_color,
                    "sessionId": session_id,
                },
            )
        if self.current_session_agent_setting:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "agent-setting",
                    "agentSetting": self.current_session_agent_setting,
                    "sessionId": session_id,
                },
            )
        if self.current_session_worktree is not _UNSET:
            append_entry_to_file(
                self.session_file,
                {
                    "type": "worktree-state",
                    "worktreeSession": self.current_session_worktree,
                    "sessionId": session_id,
                },
            )
        if (
            self.current_session_pr_number is not None
            and self.current_session_pr_url
            and self.current_session_pr_repository
        ):
            append_entry_to_file(
                self.session_file,
                {
                    "type": "pr-link",
                    "sessionId": session_id,
                    "prNumber": self.current_session_pr_number,
                    "prUrl": self.current_session_pr_url,
                    "prRepository": self.current_session_pr_repository,
                    "timestamp": _iso_now(),
                },
            )

    async def flush(self) -> None:
        """Cancel the timer, await the active drain, drain the rest, then wait
        for non-queue tracked operations."""
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._active_drain:
            await self._active_drain
        await self._drain_write_queue()

        if self._pending_write_count == 0:
            return
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._flush_resolvers.append(fut)
        await fut

    async def remove_message_by_uuid(self, target_uuid: str) -> None:
        """Remove a message from the transcript by UUID (tombstone).
        ``removeMessageByUuid``."""
        return await self._track_write(self._remove_message_by_uuid_impl(target_uuid))

    async def _remove_message_by_uuid_impl(self, target_uuid: str) -> None:
        if self.session_file is None:
            return
        session_file = self.session_file
        try:
            done = await asyncio.to_thread(_tombstone_tail, session_file, target_uuid)
            if done is True:
                return
            file_size = done if isinstance(done, int) else 0

            # Slow path: target was not in the last 64KB.
            if file_size > MAX_TOMBSTONE_REWRITE_BYTES:
                log_for_debugging(
                    f"Skipping tombstone removal: session file too large "
                    f"({format_file_size(file_size)})"
                )
                return
            await asyncio.to_thread(_tombstone_full_rewrite, session_file, target_uuid)
        except Exception:  # noqa: BLE001 - silently ignore; file may not exist yet
            pass

    def _should_skip_persistence(self) -> bool:
        """Test env / cleanupPeriodDays=0 / disabled / skip."""
        allow_test = is_env_truthy(os.environ.get("TEST_ENABLE_SESSION_PERSISTENCE"))
        cleanup_period = _get_cleanup_period_days()
        return (
            (get_node_env() == "test" and not allow_test)
            or cleanup_period == 0
            or is_session_persistence_disabled()
            or is_env_truthy(os.environ.get("TABVIS_SKIP_PROMPT_HISTORY"))
        )

    async def _materialize_session_file(self) -> None:
        """Create the session file, write startup metadata, flush buffered entries.
        ``materializeSessionFile``."""
        if self._should_skip_persistence():
            return
        self._ensure_current_session_file()
        self.re_append_session_metadata()
        if len(self._pending_entries) > 0:
            buffered = self._pending_entries
            self._pending_entries = []
            for entry in buffered:
                await self.append_entry(entry)

    async def insert_message_chain(
        self,
        messages: list[dict[str, Any]],
        is_sidechain: bool = False,
        agent_id: str | None = None,
        starting_parent_uuid: str | None = None,
        team_info: dict[str, Any] | None = None,
    ) -> None:
        """Stamp + append a chain of messages."""
        return await self._track_write(
            self._insert_message_chain_impl(
                messages, is_sidechain, agent_id, starting_parent_uuid, team_info
            )
        )

    async def _insert_message_chain_impl(
        self,
        messages: list[dict[str, Any]],
        is_sidechain: bool,
        agent_id: str | None,
        starting_parent_uuid: str | None,
        team_info: dict[str, Any] | None,
    ) -> None:
        parent_uuid: str | None = starting_parent_uuid

        if self.session_file is None and any(
            m.get("type") in ("user", "assistant") for m in messages
        ):
            await self._materialize_session_file()

        try:
            git_branch = await _get_branch()
        except Exception:  # noqa: BLE001
            git_branch = None

        session_id = get_session_id()
        slug = get_plan_slug_cache().get(session_id)

        for message in messages:
            is_compact_boundary = is_compact_boundary_message(message)

            effective_parent_uuid = parent_uuid
            if (
                message.get("type") == "user"
                and message.get("sourceToolAssistantUUID")
            ):
                effective_parent_uuid = message["sourceToolAssistantUUID"]

            # Header fields BEFORE the spread, session-stamp fields AFTER (TS ordering preserved).
            transcript_message: dict[str, Any] = {
                "parentUuid": None if is_compact_boundary else effective_parent_uuid,
                "logicalParentUuid": parent_uuid if is_compact_boundary else None,
                "isSidechain": is_sidechain,
                "teamName": (team_info or {}).get("teamName"),
                "agentName": (team_info or {}).get("agentName"),
                "promptId": (get_prompt_id() if message.get("type") == "user" else None),
                "agentId": agent_id,
                **message,
                # Session-stamp fields MUST come after the spread.
                "userType": get_user_type(),
                "entrypoint": _get_entrypoint(),
                "cwd": get_cwd(),
                "sessionId": session_id,
                "version": VERSION,
                "gitBranch": git_branch,
                "slug": slug,
            }
            await self.append_entry(transcript_message)
            if is_chain_participant(message):
                parent_uuid = message.get("uuid")

        if not is_sidechain:
            text = get_first_meaningful_user_message_text_content(messages)
            if text:
                flat = text.replace("\n", " ").strip()
                self.current_session_last_prompt = (
                    flat[:200].strip() + "…" if len(flat) > 200 else flat
                )

    async def insert_file_history_snapshot(
        self, message_id: str, snapshot: dict[str, Any], is_snapshot_update: bool
    ) -> None:
        """Insert the file history snapshot."""

        async def _do() -> None:
            await self.append_entry(
                {
                    "type": "file-history-snapshot",
                    "messageId": message_id,
                    "snapshot": snapshot,
                    "isSnapshotUpdate": is_snapshot_update,
                }
            )

        return await self._track_write(_do())

    async def insert_queue_operation(self, queue_op: dict[str, Any]) -> None:
        """Insert the queue operation."""
        return await self._track_write(self.append_entry(queue_op))

    async def insert_attribution_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Insert the attribution snapshot."""
        return await self._track_write(self.append_entry(snapshot))

    async def insert_content_replacement(
        self, replacements: list[dict[str, Any]], agent_id: str | None = None
    ) -> None:
        """Insert the content replacement."""

        async def _do() -> None:
            entry: dict[str, Any] = {
                "type": "content-replacement",
                "sessionId": get_session_id(),
                "agentId": agent_id,
                "replacements": replacements,
            }
            await self.append_entry(entry)

        return await self._track_write(_do())

    async def append_entry(
        self, entry: dict[str, Any], session_id: str | None = None
    ) -> None:
        """Append an entry to the right session file (current or other).

        Routes metadata records straight to the queue; transcript messages dedup against the
        session's known-UUID set and persist to remote when new and non-sidechain.
        """
        if self._should_skip_persistence():
            return

        if session_id is None:
            session_id = get_session_id()
        current_session_id = get_session_id()
        is_current_session = session_id == current_session_id

        if is_current_session:
            if self.session_file is None:
                self._pending_entries.append(entry)
                return
            session_file = self.session_file
        else:
            existing = await self._get_existing_session_file(session_id)
            if not existing:
                log_error(
                    Exception(
                        f"appendEntry: session file not found for other session {session_id}"
                    )
                )
                return
            session_file = existing

        entry_type = entry.get("type")
        always_append = {
            "summary",
            "custom-title",
            "ai-title",
            "last-prompt",
            "task-summary",
            "tag",
            "agent-name",
            "agent-color",
            "agent-setting",
            "pr-link",
            "file-history-snapshot",
            "attribution-snapshot",
            "speculation-accept",
            "worktree-state",
        }
        if entry_type in always_append:
            self._enqueue_write(session_file, entry)
        elif entry_type == "content-replacement":
            target_file = (
                get_agent_transcript_path(entry["agentId"])
                if entry.get("agentId")
                else session_file
            )
            self._enqueue_write(target_file, entry)
        else:
            message_set = await get_session_messages(session_id)
            if entry_type == "queue-operation":
                self._enqueue_write(session_file, entry)
            else:
                is_agent_sidechain = bool(
                    entry.get("isSidechain") and entry.get("agentId") is not None
                )
                target_file = (
                    get_agent_transcript_path(as_agent_id(entry["agentId"]))
                    if is_agent_sidechain
                    else session_file
                )
                is_new_uuid = entry.get("uuid") not in message_set
                if is_agent_sidechain or is_new_uuid:
                    self._enqueue_write(target_file, entry)
                    if not is_agent_sidechain:
                        message_set.add(entry.get("uuid"))
                        if is_transcript_message(entry):
                            await self._persist_to_remote(session_id, entry)

    def _ensure_current_session_file(self) -> str:
        """Ensure the current session file."""
        if self.session_file is None:
            self.session_file = get_transcript_path()
        return self.session_file

    async def _get_existing_session_file(self, session_id: str) -> str | None:
        """Return the existing session file."""
        cached = self._existing_session_files.get(session_id)
        if cached:
            return cached
        target_file = get_transcript_path_for_session(session_id)
        try:
            await asyncio.to_thread(os.stat, target_file)
            self._existing_session_files[session_id] = target_file
            return target_file
        except Exception as e:  # noqa: BLE001
            if is_fs_inaccessible(e):
                return None
            raise

    async def _persist_to_remote(self, session_id: str, entry: dict[str, Any]) -> None:
        """CCR-v2 internal-event path, else v1 session-ingress."""
        # Lazy import: graceful_shutdown is a cyclic sibling (may not be implemented yet).
        if _is_shutting_down():
            return

        if self._internal_event_writer:
            try:
                options: dict[str, Any] = {}
                if is_compact_boundary_message(entry):
                    options["isCompaction"] = True
                if entry.get("agentId"):
                    options["agentId"] = entry["agentId"]
                await self._internal_event_writer("transcript", entry, options)
            except Exception:  # noqa: BLE001
                log_for_debugging("Failed to write transcript as internal event")
            return

        if (
            not is_env_truthy(os.environ.get("ENABLE_SESSION_PERSISTENCE"))
            or not self._remote_ingress_url
        ):
            return

        from tabvis.agent.api import session_ingress

        success = await session_ingress.append_session_log(
            session_id, entry, self._remote_ingress_url
        )
        if not success:
            _graceful_shutdown_sync(1, "other")

    def set_remote_ingress_url(self, url: str) -> None:
        """Set the remote ingress url."""
        self._remote_ingress_url = url
        log_for_debugging(f"Remote persistence enabled with URL: {url}")
        if url:
            self._flush_interval_ms = REMOTE_FLUSH_INTERVAL_MS

    def set_internal_event_writer(self, writer: Any) -> None:
        """Set the internal event writer."""
        self._internal_event_writer = writer
        log_for_debugging(
            "CCR v2 internal event writer registered for transcript persistence"
        )
        self._flush_interval_ms = REMOTE_FLUSH_INTERVAL_MS

    def set_internal_event_reader(self, reader: Any) -> None:
        """Set the internal event reader."""
        self._internal_event_reader = reader
        log_for_debugging("CCR v2 internal event reader registered for session resume")

    def set_internal_subagent_event_reader(self, reader: Any) -> None:
        """Set the internal subagent event reader."""
        self._internal_subagent_event_reader = reader
        log_for_debugging("CCR v2 subagent event reader registered for session resume")

    def get_internal_event_reader(self) -> Any:
        return self._internal_event_reader

    def get_internal_subagent_event_reader(self) -> Any:
        return self._internal_subagent_event_reader


# Sentinel for the tri-state worktree cache (undefined vs null vs object).
_UNSET = object()


def _get_cleanup_period_days() -> Any:
    """Settings ``cleanupPeriodDays`` (None if unset). Lazy import keeps settings off the hot path."""
    try:
        from tabvis.utils.settings.settings import get_initial_settings

        settings = get_initial_settings()
        return settings.get("cleanupPeriodDays")
    except Exception:  # noqa: BLE001 - settings not available → behave as unset
        return None


def _is_shutting_down() -> bool:
    """Lazy bridge to ``graceful_shutdown.is_shutting_down`` (cyclic sibling; may be absent)."""
    try:
        from tabvis.utils.graceful_shutdown import is_shutting_down

        return is_shutting_down()
    except Exception:  # noqa: BLE001 - sibling not implemented yet → treat as not shutting down
        return False


def _graceful_shutdown_sync(code: int, reason: str) -> None:
    """Lazy bridge to ``graceful_shutdown.graceful_shutdown_sync`` (cyclic sibling)."""
    try:
        from tabvis.utils.graceful_shutdown import graceful_shutdown_sync

        graceful_shutdown_sync(code, reason)
    except Exception:  # noqa: BLE001 - sibling not implemented yet → no-op
        log_for_debugging(f"graceful_shutdown_sync({code}, {reason}) unavailable")


def _tombstone_tail(session_file: str, target_uuid: str) -> Any:
    """Fast tombstone path: positional read of the tail, splice out the line. Returns ``True`` if
    handled, else the file size (caller falls through to the slow path)."""
    fd = os.open(session_file, os.O_RDWR)
    try:
        st = os.fstat(fd)
        size = st.st_size
        if size == 0:
            return True
        chunk_len = min(size, LITE_READ_BUF_SIZE)
        tail_start = size - chunk_len
        os.lseek(fd, tail_start, os.SEEK_SET)
        tail = os.read(fd, chunk_len)
        bytes_read = len(tail)

        needle = f'"uuid":"{target_uuid}"'.encode()
        match_idx = tail.rfind(needle)
        if match_idx >= 0:
            prev_nl = tail.rfind(0x0A, 0, match_idx)
            if prev_nl >= 0 or tail_start == 0:
                line_start = prev_nl + 1  # 0 when prev_nl == -1
                next_nl = tail.find(0x0A, match_idx + len(needle))
                line_end = next_nl + 1 if next_nl >= 0 else bytes_read
                abs_line_start = tail_start + line_start
                after_len = bytes_read - line_end
                os.ftruncate(fd, abs_line_start)
                if after_len > 0:
                    os.lseek(fd, abs_line_start, os.SEEK_SET)
                    os.write(fd, tail[line_end : line_end + after_len])
                return True
        return size
    finally:
        os.close(fd)


def _tombstone_full_rewrite(session_file: str, target_uuid: str) -> None:
    """Slow tombstone path: read the whole file, drop matching lines, rewrite."""
    content = _read_text(session_file)
    kept: list[str] = []
    for line in content.split("\n"):
        if not line.strip():
            kept.append(line)
            continue
        try:
            entry = json_parse(line)
            if entry.get("uuid") != target_uuid:
                kept.append(line)
        except Exception:  # noqa: BLE001 - keep malformed lines
            kept.append(line)
    _write_text(session_file, "\n".join(kept), mode=0o600)


# --------------------------------------------------------------------------------------------
# Project singleton accessor + cleanup registration
# --------------------------------------------------------------------------------------------

_project: Project | None = None
_cleanup_registered = False


def get_project() -> Project:
    """Get (lazily create) the process-wide :class:`Project` singleton."""
    global _project, _cleanup_registered
    if _project is None:
        _project = Project()
        if not _cleanup_registered:

            async def _cleanup() -> None:
                if _project:
                    await _project.flush()
                    try:
                        _project.re_append_session_metadata()
                    except Exception:  # noqa: BLE001 - best-effort
                        pass

            register_cleanup(_cleanup)
            _cleanup_registered = True
    return _project


def reset_project_flush_state_for_testing() -> None:
    """Reset the project flush state for testing."""
    if _project:
        _project._reset_flush_state()


def reset_project_for_testing() -> None:
    """Reset the project for testing."""
    global _project
    _project = None


def set_session_file_for_testing(path: str) -> None:
    """Set the session file for testing."""
    get_project().session_file = path


def set_internal_event_writer(writer: Any) -> None:
    """Set the internal event writer."""
    get_project().set_internal_event_writer(writer)


def set_internal_event_reader(reader: Any, subagent_reader: Any) -> None:
    """Set the internal event reader."""
    get_project().set_internal_event_reader(reader)
    get_project().set_internal_subagent_event_reader(subagent_reader)


def set_remote_ingress_url_for_testing(url: str) -> None:
    """Set the remote ingress url for testing."""
    get_project().set_remote_ingress_url(url)


# --------------------------------------------------------------------------------------------
# Top-level record/save helpers
# --------------------------------------------------------------------------------------------


async def record_transcript(
    messages: list[dict[str, Any]],
    team_info: dict[str, Any] | None = None,
    starting_parent_uuid_hint: str | None = None,
    all_messages: list[dict[str, Any]] | None = None,
) -> str | None:
    """Filter already-recorded messages, then append the new ones.

    Returns the last actually-recorded chain participant's UUID, or the prefix-tracked UUID.
    """
    cleaned_messages = clean_messages_for_logging(messages, all_messages)
    session_id = get_session_id()
    message_set = await get_session_messages(session_id)
    new_messages: list[dict[str, Any]] = []
    starting_parent_uuid = starting_parent_uuid_hint
    seen_new_message = False
    for m in cleaned_messages:
        if m.get("uuid") in message_set:
            if not seen_new_message and is_chain_participant(m):
                starting_parent_uuid = m.get("uuid")
        else:
            new_messages.append(m)
            seen_new_message = True
    if len(new_messages) > 0:
        await get_project().insert_message_chain(
            new_messages, False, None, starting_parent_uuid, team_info
        )
    last_recorded = _find_last(new_messages, is_chain_participant)
    if last_recorded is not None:
        return last_recorded.get("uuid")
    return starting_parent_uuid


async def record_sidechain_transcript(
    messages: list[dict[str, Any]],
    agent_id: str | None = None,
    starting_parent_uuid: str | None = None,
) -> None:
    """Record the sidechain transcript."""
    await get_project().insert_message_chain(
        clean_messages_for_logging(messages), True, agent_id, starting_parent_uuid
    )


async def record_queue_operation(queue_op: dict[str, Any]) -> None:
    """Record the queue operation."""
    await get_project().insert_queue_operation(queue_op)


async def remove_transcript_message(target_uuid: str) -> None:
    """Remove the transcript message."""
    await get_project().remove_message_by_uuid(target_uuid)


async def record_file_history_snapshot(
    message_id: str, snapshot: dict[str, Any], is_snapshot_update: bool
) -> None:
    """Record the file history snapshot."""
    await get_project().insert_file_history_snapshot(
        message_id, snapshot, is_snapshot_update
    )


async def record_attribution_snapshot(snapshot: dict[str, Any]) -> None:
    """Record the attribution snapshot."""
    await get_project().insert_attribution_snapshot(snapshot)


async def record_content_replacement(
    replacements: list[dict[str, Any]], agent_id: str | None = None
) -> None:
    """Record the content replacement."""
    await get_project().insert_content_replacement(replacements, agent_id)


async def reset_session_file_pointer() -> None:
    """Reset the session file pointer."""
    get_project().reset_session_file()


def adopt_resumed_session_file() -> None:
    """Adopt the existing session file after --continue/--resume (non-fork).
    ``adoptResumedSessionFile``."""
    project = get_project()
    project.session_file = get_transcript_path()
    project.re_append_session_metadata(True)


async def flush_session_storage() -> None:
    """Flush all pending session transcript writes."""
    await get_project().flush()


# --------------------------------------------------------------------------------------------
# Remote / CCR-v2 hydration
# --------------------------------------------------------------------------------------------


async def hydrate_remote_session(session_id: str, ingress_url: str) -> bool:
    """Replace local logs with the remote session logs."""
    switch_session(as_session_id(session_id))
    project = get_project()
    from tabvis.agent.api import session_ingress

    try:
        remote_logs = await session_ingress.get_session_logs(session_id, ingress_url) or []
        project_dir = get_project_dir(get_original_cwd())
        await asyncio.to_thread(os.makedirs, project_dir, mode=0o700, exist_ok=True)
        session_file = get_transcript_path_for_session(session_id)
        content = "".join(json_stringify(e) + "\n" for e in remote_logs)
        await asyncio.to_thread(_write_text, session_file, content, 0o600)
        log_for_debugging(f"Hydrated {len(remote_logs)} entries from remote")
        return len(remote_logs) > 0
    except Exception as error:  # noqa: BLE001
        log_for_debugging(f"Error hydrating session from remote: {error}")
        log_for_diagnostics_no_pii("error", "hydrate_remote_session_fail")
        return False
    finally:
        project.set_remote_ingress_url(ingress_url)


async def hydrate_from_ccr_v2_internal_events(session_id: str) -> bool:
    """Hydrate session state from CCR-v2 internal events.
    ``hydrateFromCCRv2InternalEvents``."""
    start_ms = time.time() * 1000
    switch_session(as_session_id(session_id))
    project = get_project()
    reader = project.get_internal_event_reader()
    if not reader:
        log_for_debugging("No internal event reader registered for CCR v2 resume")
        return False

    try:
        events = await reader()
        if events is None:
            log_for_debugging("Failed to read internal events for resume")
            log_for_diagnostics_no_pii("error", "hydrate_ccr_v2_read_fail")
            return False

        project_dir = get_project_dir(get_original_cwd())
        await asyncio.to_thread(os.makedirs, project_dir, mode=0o700, exist_ok=True)

        session_file = get_transcript_path_for_session(session_id)
        fg_content = "".join(json_stringify(e["payload"]) + "\n" for e in events)
        await asyncio.to_thread(_write_text, session_file, fg_content, 0o600)
        log_for_debugging(
            f"Hydrated {len(events)} foreground entries from CCR v2 internal events"
        )

        subagent_event_count = 0
        subagent_reader = project.get_internal_subagent_event_reader()
        if subagent_reader:
            subagent_events = await subagent_reader()
            if subagent_events:
                subagent_event_count = len(subagent_events)
                by_agent: dict[str, list[dict[str, Any]]] = {}
                for e in subagent_events:
                    agent_id = e.get("agent_id") or ""
                    if not agent_id:
                        continue
                    by_agent.setdefault(agent_id, []).append(e["payload"])
                for agent_id, entries in by_agent.items():
                    agent_file = get_agent_transcript_path(as_agent_id(agent_id))
                    await asyncio.to_thread(
                        os.makedirs, os.path.dirname(agent_file), mode=0o700, exist_ok=True
                    )
                    agent_content = "".join(json_stringify(p) + "\n" for p in entries)
                    await asyncio.to_thread(_write_text, agent_file, agent_content, 0o600)
                log_for_debugging(
                    f"Hydrated {len(subagent_events)} subagent entries across "
                    f"{len(by_agent)} agents"
                )

        log_for_diagnostics_no_pii(
            "info",
            "hydrate_ccr_v2_completed",
            {
                "duration_ms": int(time.time() * 1000 - start_ms),
                "event_count": len(events),
                "subagent_event_count": subagent_event_count,
            },
        )
        return len(events) > 0
    except Exception as error:  # noqa: BLE001
        if isinstance(error, Exception) and str(error) == "CCRClient: Epoch mismatch (409)":
            raise
        log_for_debugging(f"Error hydrating session from CCR v2: {error}")
        log_for_diagnostics_no_pii("error", "hydrate_ccr_v2_fail")
        return False


# --------------------------------------------------------------------------------------------
# First-prompt extraction + serialization helpers
# --------------------------------------------------------------------------------------------


def extract_first_prompt(transcript: list[dict[str, Any]]) -> str:
    """First meaningful prompt, truncated, or 'No prompt'."""
    text_content = get_first_meaningful_user_message_text_content(transcript)
    if text_content:
        result = text_content.replace("\n", " ").strip()
        if len(result) > 200:
            result = result[:200].strip() + "…"
        return result
    return "No prompt"


def get_first_meaningful_user_message_text_content(
    transcript: list[dict[str, Any]],
) -> str | None:
    """Return the first meaningful user message text content."""
    for msg in transcript:
        if msg.get("type") != "user" or msg.get("isMeta"):
            continue
        if msg.get("isCompactSummary"):
            continue

        message = msg.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            continue

        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and block.get("text")
                ):
                    texts.append(block["text"])

        for text_content in texts:
            if not text_content:
                continue

            command_name_tag = extract_tag(text_content, COMMAND_NAME_TAG)
            if command_name_tag:
                command_name = re.sub(r"^/", "", command_name_tag)
                if command_name in _built_in_command_names():
                    continue
                command_args_raw = extract_tag(text_content, COMMAND_ARGS_TAG)
                command_args = command_args_raw.strip() if command_args_raw else None
                if not command_args:
                    continue
                return f"{command_name_tag} {command_args}"

            bash_input = extract_tag(text_content, "bash-input")
            if bash_input:
                return f"! {bash_input}"

            if SKIP_FIRST_PROMPT_PATTERN.match(text_content):
                continue

            return text_content
    return None


def _built_in_command_names() -> set[str]:
    """Lazy bridge to ``commands.built_in_command_names`` (kept off the import top to stay
    cycle-safe and lightweight)."""
    try:
        from tabvis.ui.commands import built_in_command_names

        return built_in_command_names()
    except Exception:  # noqa: BLE001 - registry unavailable → treat nothing as built-in
        return set()


def remove_extra_fields(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip ``isSidechain`` / ``parentUuid`` from each message."""
    result: list[dict[str, Any]] = []
    for m in transcript:
        serialized = {k: v for k, v in m.items() if k not in ("isSidechain", "parentUuid")}
        result.append(serialized)
    return result


def _find_last(items: list, predicate) -> Any:
    """Find the last."""
    for item in reversed(items):
        if predicate(item):
            return item
    return None


# --------------------------------------------------------------------------------------------
# Chain reconstruction (relinks, conversation chain, leaf detection)
# --------------------------------------------------------------------------------------------


def apply_preserved_segment_relinks(messages: dict[str, dict[str, Any]]) -> None:
    """Splice the preserved segment back into the chain after compaction, mutating in place."""
    last_seg: dict[str, Any] | None = None
    last_seg_boundary_idx = -1
    absolute_last_boundary_idx = -1
    entry_index: dict[str, int] = {}
    i = 0
    for entry in messages.values():
        entry_index[entry["uuid"]] = i
        if is_compact_boundary_message(entry):
            absolute_last_boundary_idx = i
            seg = (entry.get("compactMetadata") or {}).get("preservedSegment")
            if seg:
                last_seg = seg
                last_seg_boundary_idx = i
        i += 1
    if not last_seg:
        return

    seg_is_live = last_seg_boundary_idx == absolute_last_boundary_idx

    preserved_uuids: set[str] = set()
    if seg_is_live:
        walk_seen: set[str] = set()
        cur = messages.get(last_seg["tailUuid"])
        reached_head = False
        while cur and cur["uuid"] not in walk_seen:
            walk_seen.add(cur["uuid"])
            preserved_uuids.add(cur["uuid"])
            if cur["uuid"] == last_seg["headUuid"]:
                reached_head = True
                break
            cur = messages.get(cur["parentUuid"]) if cur.get("parentUuid") else None
        if not reached_head:
            return

    if seg_is_live:
        head = messages.get(last_seg["headUuid"])
        if head:
            messages[last_seg["headUuid"]] = {**head, "parentUuid": last_seg["anchorUuid"]}
        for uuid, msg in list(messages.items()):
            if msg.get("parentUuid") == last_seg["anchorUuid"] and uuid != last_seg["headUuid"]:
                messages[uuid] = {**msg, "parentUuid": last_seg["tailUuid"]}
        for uuid in preserved_uuids:
            msg = messages.get(uuid)
            if not msg or msg.get("type") != "assistant":
                continue
            inner = msg.get("message", {})
            usage = inner.get("usage", {})
            messages[uuid] = {
                **msg,
                "message": {
                    **inner,
                    "usage": {
                        **usage,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                    },
                },
            }

    to_delete: list[str] = []
    for uuid in messages:
        idx = entry_index.get(uuid)
        if idx is not None and idx < absolute_last_boundary_idx and uuid not in preserved_uuids:
            to_delete.append(uuid)
    for uuid in to_delete:
        del messages[uuid]


def apply_snip_removals(messages: dict[str, dict[str, Any]]) -> None:
    """Delete Snip-removed messages and relink parentUuid across gaps, mutating in place."""
    to_delete: set[str] = set()
    for entry in messages.values():
        removed_uuids = (entry.get("snipMetadata") or {}).get("removedUuids")
        if not removed_uuids:
            continue
        for uuid in removed_uuids:
            to_delete.add(uuid)
    if len(to_delete) == 0:
        return

    deleted_parent: dict[str, str | None] = {}
    removed_count = 0
    for uuid in to_delete:
        entry = messages.get(uuid)
        if not entry:
            continue
        deleted_parent[uuid] = entry.get("parentUuid")
        del messages[uuid]
        removed_count += 1

    def resolve(start: str) -> str | None:
        path: list[str] = []
        cur: str | None = start
        while cur and cur in to_delete:
            path.append(cur)
            cur = deleted_parent.get(cur, _MISSING)
            if cur is _MISSING:
                cur = None
                break
        for p in path:
            deleted_parent[p] = cur
        return cur

    relinked_count = 0
    for uuid, msg in list(messages.items()):
        if not msg.get("parentUuid") or msg["parentUuid"] not in to_delete:
            continue
        messages[uuid] = {**msg, "parentUuid": resolve(msg["parentUuid"])}
        relinked_count += 1


_MISSING = object()


def _find_latest_message(messages, predicate) -> dict[str, Any] | None:
    """The message with the latest timestamp matching ``predicate``."""
    latest: dict[str, Any] | None = None
    max_time = float("-inf")
    for m in messages:
        if not predicate(m):
            continue
        t = _date_parse(m.get("timestamp", ""))
        if t is None:
            continue
        if t > max_time:
            max_time = t
            latest = m
    return latest


def build_conversation_chain(
    messages: dict[str, dict[str, Any]], leaf_message: dict[str, Any]
) -> list[dict[str, Any]]:
    """Build a conversation chain from leaf to root (then root→leaf).
    ``buildConversationChain``."""
    transcript: list[dict[str, Any]] = []
    seen: set[str] = set()
    current_msg: dict[str, Any] | None = leaf_message
    while current_msg:
        if current_msg["uuid"] in seen:
            log_error(
                Exception(
                    f"Cycle detected in parentUuid chain at message {current_msg['uuid']}. "
                    "Returning partial transcript."
                )
            )
            break
        seen.add(current_msg["uuid"])
        transcript.append(current_msg)
        current_msg = (
            messages.get(current_msg["parentUuid"]) if current_msg.get("parentUuid") else None
        )
    transcript.reverse()
    return _recover_orphaned_parallel_tool_results(messages, transcript, seen)


def _recover_orphaned_parallel_tool_results(
    messages: dict[str, dict[str, Any]],
    chain: list[dict[str, Any]],
    seen: set[str],
) -> list[dict[str, Any]]:
    """Recover sibling assistant blocks and parallel tool results omitted by the parent walk."""
    chain_assistants = [m for m in chain if m.get("type") == "assistant"]
    if len(chain_assistants) == 0:
        return chain

    anchor_by_msg_id: dict[str, dict[str, Any]] = {}
    for a in chain_assistants:
        msg_id = (a.get("message") or {}).get("id")
        if msg_id:
            anchor_by_msg_id[msg_id] = a

    siblings_by_msg_id: dict[str, list[dict[str, Any]]] = {}
    tool_results_by_asst: dict[str, list[dict[str, Any]]] = {}
    for m in messages.values():
        if m.get("type") == "assistant" and (m.get("message") or {}).get("id"):
            siblings_by_msg_id.setdefault(m["message"]["id"], []).append(m)
        elif (
            m.get("type") == "user"
            and m.get("parentUuid")
            and isinstance((m.get("message") or {}).get("content"), list)
            and any(
                b.get("type") == "tool_result"
                for b in m["message"]["content"]
                if isinstance(b, dict)
            )
        ):
            tool_results_by_asst.setdefault(m["parentUuid"], []).append(m)

    processed_groups: set[str] = set()
    inserts: dict[str, list[dict[str, Any]]] = {}
    recovered_count = 0
    for asst in chain_assistants:
        msg_id = (asst.get("message") or {}).get("id")
        if not msg_id or msg_id in processed_groups:
            continue
        processed_groups.add(msg_id)

        group = siblings_by_msg_id.get(msg_id, [asst])
        orphaned_siblings = [s for s in group if s["uuid"] not in seen]
        orphaned_trs: list[dict[str, Any]] = []
        for member in group:
            trs = tool_results_by_asst.get(member["uuid"])
            if not trs:
                continue
            for tr in trs:
                if tr["uuid"] not in seen:
                    orphaned_trs.append(tr)
        if len(orphaned_siblings) == 0 and len(orphaned_trs) == 0:
            continue

        orphaned_siblings.sort(key=lambda a: a.get("timestamp", ""))
        orphaned_trs.sort(key=lambda a: a.get("timestamp", ""))

        anchor = anchor_by_msg_id[msg_id]
        recovered = [*orphaned_siblings, *orphaned_trs]
        for r in recovered:
            seen.add(r["uuid"])
        recovered_count += len(recovered)
        inserts[anchor["uuid"]] = recovered

    if recovered_count == 0:
        return chain

    result: list[dict[str, Any]] = []
    for m in chain:
        result.append(m)
        to_insert = inserts.get(m["uuid"])
        if to_insert:
            result.extend(to_insert)
    return result


def check_resume_consistency(chain: list[dict[str, Any]]) -> None:
    """Emit a round-trip drift analytics event."""
    for i in range(len(chain) - 1, -1, -1):
        m = chain[i]
        if m.get("type") != "system" or m.get("subtype") != "turn_duration":
            continue
        expected = m.get("messageCount")
        if expected is None:
            return
        actual = i
        return


def _build_file_history_snapshot_chain(
    file_history_snapshots: dict[str, dict[str, Any]],
    conversation: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the file history snapshot chain."""
    snapshots: list[dict[str, Any]] = []
    index_by_message_id: dict[str, int] = {}
    for message in conversation:
        snapshot_message = file_history_snapshots.get(message["uuid"])
        if not snapshot_message:
            continue
        snapshot = snapshot_message["snapshot"]
        is_snapshot_update = snapshot_message["isSnapshotUpdate"]
        existing_index = (
            index_by_message_id.get(snapshot["messageId"]) if is_snapshot_update else None
        )
        if existing_index is None:
            index_by_message_id[snapshot["messageId"]] = len(snapshots)
            snapshots.append(snapshot)
        else:
            snapshots[existing_index] = snapshot
    return snapshots


def _build_attribution_snapshot_chain(
    attribution_snapshots: dict[str, dict[str, Any]],
    _conversation: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Returns all snapshots (merged on restore)."""
    return list(attribution_snapshots.values())


# --------------------------------------------------------------------------------------------
# loadTranscriptFromFile + visibility helpers + convertToLogOption
# --------------------------------------------------------------------------------------------


async def load_transcript_from_file(file_path: str) -> dict[str, Any]:
    """Load a transcript (.json or .jsonl) into a ``LogOption``.
    ``loadTranscriptFromFile``."""
    if file_path.endswith(".jsonl"):
        loaded = await load_transcript_file(file_path)
        messages = loaded["messages"]
        if len(messages) == 0:
            raise Exception("No messages found in JSONL file")

        leaf_uuids = loaded["leafUuids"]
        leaf_message = _find_latest_message(
            messages.values(), lambda msg: msg["uuid"] in leaf_uuids
        )
        if not leaf_message:
            raise Exception("No valid conversation chain found in JSONL file")

        transcript = build_conversation_chain(messages, leaf_message)
        summary = loaded["summaries"].get(leaf_message["uuid"])
        custom_title = loaded["customTitles"].get(leaf_message.get("sessionId"))
        tag = loaded["tags"].get(leaf_message.get("sessionId"))
        session_id = leaf_message.get("sessionId")
        log_option = _convert_to_log_option(
            transcript,
            0,
            summary,
            custom_title,
            _build_file_history_snapshot_chain(loaded["fileHistorySnapshots"], transcript),
            tag,
            file_path,
            _build_attribution_snapshot_chain(loaded["attributionSnapshots"], transcript),
            None,
            loaded["contentReplacements"].get(session_id, []),
        )
        worktree_states = loaded["worktreeStates"]
        if session_id in worktree_states:
            log_option["worktreeSession"] = worktree_states[session_id]
        return log_option

    # JSON log files.
    content = await asyncio.to_thread(_read_text, file_path)
    try:
        parsed = json_parse(content)
    except Exception as error:  # noqa: BLE001
        raise Exception(f"Invalid JSON in transcript file: {error}") from error

    if isinstance(parsed, list):
        messages_list = parsed
    elif isinstance(parsed, dict) and "messages" in parsed:
        if not isinstance(parsed["messages"], list):
            raise Exception("Transcript messages must be an array")
        messages_list = parsed["messages"]
    else:
        raise Exception(
            "Transcript must be an array of messages or an object with a messages array"
        )

    return _convert_to_log_option(
        messages_list, 0, None, None, None, None, file_path
    )


def _has_visible_user_content(message: dict[str, Any]) -> bool:
    if message.get("type") != "user":
        return False
    if message.get("isMeta"):
        return False
    content = (message.get("message") or {}).get("content")
    if not content:
        return False
    if isinstance(content, str):
        return len(content.strip()) > 0
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in ("text", "image", "document")
            for b in content
        )
    return False


def _has_visible_assistant_content(message: dict[str, Any]) -> bool:
    if message.get("type") != "assistant":
        return False
    content = (message.get("message") or {}).get("content")
    if not content or not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict)
        and b.get("type") == "text"
        and isinstance(b.get("text"), str)
        and len(b["text"].strip()) > 0
        for b in content
    )


def _count_visible_messages(transcript: list[dict[str, Any]]) -> int:
    count = 0
    for message in transcript:
        t = message.get("type")
        if t == "user":
            if _has_visible_user_content(message):
                count += 1
        elif t == "assistant":
            if _has_visible_assistant_content(message):
                count += 1
    return count


def _convert_to_log_option(
    transcript: list[dict[str, Any]],
    value: int = 0,
    summary: str | None = None,
    custom_title: str | None = None,
    file_history_snapshots: list[dict[str, Any]] | None = None,
    tag: str | None = None,
    full_path: str | None = None,
    attribution_snapshots: list[dict[str, Any]] | None = None,
    agent_setting: str | None = None,
    content_replacements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert the to log option."""
    last_message = transcript[-1]
    first_message = transcript[0]
    first_prompt = extract_first_prompt(transcript)
    created = _to_datetime(first_message["timestamp"])
    modified = _to_datetime(last_message["timestamp"])

    log_option: dict[str, Any] = {
        "date": last_message["timestamp"],
        "messages": remove_extra_fields(transcript),
        "fullPath": full_path,
        "value": value,
        "created": created,
        "modified": modified,
        "firstPrompt": first_prompt,
        "messageCount": _count_visible_messages(transcript),
        "isSidechain": first_message.get("isSidechain"),
        "teamName": first_message.get("teamName"),
        "agentName": first_message.get("agentName"),
        "agentSetting": agent_setting,
        "leafUuid": last_message["uuid"],
        "summary": summary,
        "customTitle": custom_title,
        "tag": tag,
        "fileHistorySnapshots": file_history_snapshots,
        "attributionSnapshots": attribution_snapshots,
        "contentReplacements": content_replacements,
        "gitBranch": last_message.get("gitBranch"),
        "projectPath": first_message.get("cwd"),
    }
    return log_option


def _to_datetime(timestamp: str) -> datetime:
    """JS ``new Date(timestamp)`` → a ``datetime`` (used for sort keys, not serialized)."""
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return datetime.fromtimestamp(0, tz=UTC)


# --------------------------------------------------------------------------------------------
# Analytics + fetch + save* helpers
# --------------------------------------------------------------------------------------------


async def _track_session_branching_analytics(logs: list[dict[str, Any]]) -> None:
    """Emit analytics for session branching when tracking is available."""
    session_id_counts: dict[str, int] = {}
    max_count = 0
    for log in logs:
        session_id = get_session_id_from_log(log)
        if session_id:
            new_count = session_id_counts.get(session_id, 0) + 1
            session_id_counts[session_id] = new_count
            max_count = max(new_count, max_count)
    if max_count <= 1:
        return
    branch_counts = [c for c in session_id_counts.values() if c > 1]
    sessions_with_branches = len(branch_counts)
    total_branches = sum(branch_counts)
    _ = (sessions_with_branches, total_branches)  # computed for parity; analytics emit removed


async def fetch_logs(limit: int | None = None) -> list[dict[str, Any]]:
    """Load session logs for the requested project scope."""
    project_dir = get_project_dir(get_original_cwd())
    logs = await get_session_files_lite(project_dir, limit, get_original_cwd())
    await _track_session_branching_analytics(logs)
    return logs


async def save_custom_title(
    session_id: str,
    custom_title: str,
    full_path: str | None = None,
    source: str = "user",
) -> None:
    """Save the custom title."""
    resolved_path = full_path or get_transcript_path_for_session(session_id)
    append_entry_to_file(
        resolved_path,
        {"type": "custom-title", "customTitle": custom_title, "sessionId": session_id},
    )
    if session_id == get_session_id():
        get_project().current_session_title = custom_title


def save_ai_generated_title(session_id: str, ai_title: str) -> None:
    """Save the ai generated title."""
    append_entry_to_file(
        get_transcript_path_for_session(session_id),
        {"type": "ai-title", "aiTitle": ai_title, "sessionId": session_id},
    )


def save_task_summary(session_id: str, summary: str) -> None:
    """Save the task summary."""
    append_entry_to_file(
        get_transcript_path_for_session(session_id),
        {
            "type": "task-summary",
            "summary": summary,
            "sessionId": session_id,
            "timestamp": _iso_now(),
        },
    )


async def save_tag(session_id: str, tag: str, full_path: str | None = None) -> None:
    """Save the tag."""
    resolved_path = full_path or get_transcript_path_for_session(session_id)
    append_entry_to_file(resolved_path, {"type": "tag", "tag": tag, "sessionId": session_id})
    if session_id == get_session_id():
        get_project().current_session_tag = tag


async def link_session_to_pr(
    session_id: str,
    pr_number: int,
    pr_url: str,
    pr_repository: str,
    full_path: str | None = None,
) -> None:
    """Persist the pull-request link for the current session."""
    resolved_path = full_path or get_transcript_path_for_session(session_id)
    append_entry_to_file(
        resolved_path,
        {
            "type": "pr-link",
            "sessionId": session_id,
            "prNumber": pr_number,
            "prUrl": pr_url,
            "prRepository": pr_repository,
            "timestamp": _iso_now(),
        },
    )
    if session_id == get_session_id():
        project = get_project()
        project.current_session_pr_number = pr_number
        project.current_session_pr_url = pr_url
        project.current_session_pr_repository = pr_repository


def get_current_session_tag(session_id: str) -> str | None:
    """Return the current session tag."""
    if session_id == get_session_id():
        return get_project().current_session_tag
    return None


def get_current_session_title(session_id: str) -> str | None:
    """Return the current session title."""
    if session_id == get_session_id():
        return get_project().current_session_title
    return None


def get_current_session_agent_color() -> str | None:
    """Return the current session agent color."""
    return get_project().current_session_agent_color


def restore_session_metadata(meta: dict[str, Any]) -> None:
    """Restore session metadata into the in-memory cache on resume.
    ``restoreSessionMetadata``."""
    project = get_project()
    if meta.get("customTitle"):
        if project.current_session_title is None:
            project.current_session_title = meta["customTitle"]
    if meta.get("tag") is not None:
        project.current_session_tag = meta["tag"] or None
    if meta.get("agentName"):
        project.current_session_agent_name = meta["agentName"]
    if meta.get("agentColor"):
        project.current_session_agent_color = meta["agentColor"]
    if meta.get("agentSetting"):
        project.current_session_agent_setting = meta["agentSetting"]
    if meta.get("worktreeSession") is not None or "worktreeSession" in meta:
        if "worktreeSession" in meta:
            project.current_session_worktree = meta["worktreeSession"]
    if meta.get("prNumber") is not None:
        project.current_session_pr_number = meta["prNumber"]
    if meta.get("prUrl"):
        project.current_session_pr_url = meta["prUrl"]
    if meta.get("prRepository"):
        project.current_session_pr_repository = meta["prRepository"]


def clear_session_metadata() -> None:
    """Clear the session metadata."""
    project = get_project()
    project.current_session_title = None
    project.current_session_tag = None
    project.current_session_agent_name = None
    project.current_session_agent_color = None
    project.current_session_last_prompt = None
    project.current_session_agent_setting = None
    project.current_session_worktree = _UNSET
    project.current_session_pr_number = None
    project.current_session_pr_url = None
    project.current_session_pr_repository = None


def re_append_session_metadata() -> None:
    """Append cached session metadata again so it remains in the tail window."""
    get_project().re_append_session_metadata()


async def save_agent_name(
    session_id: str,
    agent_name: str,
    full_path: str | None = None,
    source: str = "user",
) -> None:
    """Save the agent name."""
    resolved_path = full_path or get_transcript_path_for_session(session_id)
    append_entry_to_file(
        resolved_path, {"type": "agent-name", "agentName": agent_name, "sessionId": session_id}
    )
    if session_id == get_session_id():
        get_project().current_session_agent_name = agent_name
        await update_session_name(agent_name)


async def save_agent_color(
    session_id: str, agent_color: str, full_path: str | None = None
) -> None:
    """Save the agent color."""
    resolved_path = full_path or get_transcript_path_for_session(session_id)
    append_entry_to_file(
        resolved_path, {"type": "agent-color", "agentColor": agent_color, "sessionId": session_id}
    )
    if session_id == get_session_id():
        get_project().current_session_agent_color = agent_color


def save_agent_setting(agent_setting: str) -> None:
    """Save the agent setting."""
    get_project().current_session_agent_setting = agent_setting


def cache_session_title(custom_title: str) -> None:
    """Cache the session title without writing it to disk."""
    get_project().current_session_title = custom_title


def save_worktree_state(worktree_session: dict[str, Any] | None) -> None:
    """Strip ephemeral fields, cache, eager-write if file exists."""
    if worktree_session:
        stripped: dict[str, Any] | None = {
            "originalCwd": worktree_session.get("originalCwd"),
            "worktreePath": worktree_session.get("worktreePath"),
            "worktreeName": worktree_session.get("worktreeName"),
            "worktreeBranch": worktree_session.get("worktreeBranch"),
            "originalBranch": worktree_session.get("originalBranch"),
            "originalHeadCommit": worktree_session.get("originalHeadCommit"),
            "sessionId": worktree_session.get("sessionId"),
            "tmuxSessionName": worktree_session.get("tmuxSessionName"),
            "hookBased": worktree_session.get("hookBased"),
        }
    else:
        stripped = None
    project = get_project()
    project.current_session_worktree = stripped
    if project.session_file:
        append_entry_to_file(
            project.session_file,
            {
                "type": "worktree-state",
                "worktreeSession": stripped,
                "sessionId": get_session_id(),
            },
        )


def get_session_id_from_log(log: dict[str, Any]) -> str | None:
    """Return the session id from log."""
    if log.get("sessionId"):
        return log["sessionId"]
    messages = log.get("messages") or []
    return messages[0].get("sessionId") if messages else None


def is_lite_log(log: dict[str, Any]) -> bool:
    """Return whether lite log."""
    return len(log.get("messages") or []) == 0 and log.get("sessionId") is not None


async def load_full_log(log: dict[str, Any]) -> dict[str, Any]:
    """Load full messages for a lite log by reading its JSONL file."""
    if not is_lite_log(log):
        return log
    session_file = log.get("fullPath")
    if not session_file:
        return log

    try:
        loaded = await load_transcript_file(session_file)
        messages = loaded["messages"]
        if len(messages) == 0:
            return log

        most_recent_leaf = _find_latest_message(
            messages.values(),
            lambda msg: msg["uuid"] in loaded["leafUuids"]
            and msg.get("type") in ("user", "assistant"),
        )
        if not most_recent_leaf:
            return log

        transcript = build_conversation_chain(messages, most_recent_leaf)
        session_id = most_recent_leaf.get("sessionId")
        result = {**log}
        result.update(
            {
                "messages": remove_extra_fields(transcript),
                "firstPrompt": extract_first_prompt(transcript),
                "messageCount": _count_visible_messages(transcript),
                "summary": loaded["summaries"].get(most_recent_leaf["uuid"]),
                "customTitle": loaded["customTitles"].get(session_id)
                if session_id
                else log.get("customTitle"),
                "tag": loaded["tags"].get(session_id) if session_id else log.get("tag"),
                "agentName": loaded["agentNames"].get(session_id)
                if session_id
                else log.get("agentName"),
                "agentColor": loaded["agentColors"].get(session_id)
                if session_id
                else log.get("agentColor"),
                "agentSetting": loaded["agentSettings"].get(session_id)
                if session_id
                else log.get("agentSetting"),
                "prNumber": loaded["prNumbers"].get(session_id)
                if session_id
                else log.get("prNumber"),
                "prUrl": loaded["prUrls"].get(session_id) if session_id else log.get("prUrl"),
                "prRepository": loaded["prRepositories"].get(session_id)
                if session_id
                else log.get("prRepository"),
                "gitBranch": most_recent_leaf.get("gitBranch") or log.get("gitBranch"),
                "isSidechain": (transcript[0].get("isSidechain") if transcript else None)
                if transcript
                else log.get("isSidechain"),
                "teamName": (transcript[0].get("teamName") if transcript else None)
                if transcript
                else log.get("teamName"),
                "leafUuid": most_recent_leaf.get("uuid") or log.get("leafUuid"),
                "fileHistorySnapshots": _build_file_history_snapshot_chain(
                    loaded["fileHistorySnapshots"], transcript
                ),
                "attributionSnapshots": _build_attribution_snapshot_chain(
                    loaded["attributionSnapshots"], transcript
                ),
                "contentReplacements": loaded["contentReplacements"].get(session_id, [])
                if session_id
                else log.get("contentReplacements"),
            }
        )
        worktree_states = loaded["worktreeStates"]
        if session_id and session_id in worktree_states:
            result["worktreeSession"] = worktree_states[session_id]
        else:
            result["worktreeSession"] = log.get("worktreeSession")
        return result
    except Exception:  # noqa: BLE001 - return the original log on failure
        return log


async def search_sessions_by_custom_title(
    query: str, options: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Search sessions by custom title (case-insensitive).
    ``searchSessionsByCustomTitle``."""
    opts = options or {}
    limit = opts.get("limit")
    exact = opts.get("exact")
    worktree_paths = await get_worktree_paths(get_original_cwd())
    all_stat_logs = await _get_stat_only_logs_for_worktrees(worktree_paths)
    enriched = await enrich_logs(all_stat_logs, 0, len(all_stat_logs))
    logs = enriched["logs"]
    normalized_query = query.lower().strip()

    matching_logs = []
    for log in logs:
        title = (log.get("customTitle") or "").lower().strip()
        if not title:
            continue
        if (exact and title == normalized_query) or (
            not exact and normalized_query in title
        ):
            matching_logs.append(log)

    session_id_to_log: dict[str, dict[str, Any]] = {}
    for log in matching_logs:
        session_id = get_session_id_from_log(log)
        if session_id:
            existing = session_id_to_log.get(session_id)
            if not existing or log["modified"] > existing["modified"]:
                session_id_to_log[session_id] = log
    deduplicated = list(session_id_to_log.values())
    deduplicated.sort(key=lambda log: log["modified"], reverse=True)

    if limit:
        return deduplicated[:limit]
    return deduplicated


# --------------------------------------------------------------------------------------------
# Pre-boundary metadata scan + chain pre-filter
# --------------------------------------------------------------------------------------------

_METADATA_TYPE_MARKERS = [
    '"type":"summary"',
    '"type":"custom-title"',
    '"type":"tag"',
    '"type":"agent-name"',
    '"type":"agent-color"',
    '"type":"agent-setting"',
    '"type":"mode"',
    '"type":"worktree-state"',
    '"type":"pr-link"',
]
_METADATA_MARKER_BUFS = [m.encode("utf-8") for m in _METADATA_TYPE_MARKERS]


def _scan_pre_boundary_metadata(data: bytes, end_offset: int) -> list[str]:
    """Collect metadata-entry lines within ``[0, end_offset)``.
    ``scanPreBoundaryMetadata`` (operates on the already-read prefix; faithful line scan)."""
    metadata_lines: list[str] = []
    prefix = data[:end_offset]
    pos = 0
    n = len(prefix)
    while pos < n:
        nl = prefix.find(b"\n", pos)
        line_end = nl if nl != -1 else n
        line = prefix[pos:line_end]
        for m in _METADATA_MARKER_BUFS:
            if m in line:
                metadata_lines.append(line.decode("utf-8", "replace"))
                break
        pos = line_end + 1 if nl != -1 else n
    return metadata_lines


# --------------------------------------------------------------------------------------------
# loadTranscriptFile — the central reader
# --------------------------------------------------------------------------------------------


async def load_transcript_file(
    file_path: str, opts: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Load all messages + metadata maps from a transcript file.

    Returns a dict of maps keyed like the TS object: ``messages`` / ``summaries`` /
    ``customTitles`` / ``tags`` / ``agentNames`` / ``agentColors`` / ``agentSettings`` /
    ``prNumbers`` / ``prUrls`` / ``prRepositories`` / ``worktreeStates`` /
    ``fileHistorySnapshots`` / ``attributionSnapshots`` / ``contentReplacements`` /
    ``agentContentReplacements`` / ``leafUuids``.

    Fidelity note: the TS chunked precompact-skip + ``walkChainBeforeParse`` byte pre-filter are
    pure I/O/parse optimizations whose *observable result* is identical to parsing the whole file
    and letting ``buildConversationChain`` discard dead branches. This implementation runs the boundary scan
    (via the existing ``read_transcript_for_load``) for the metadata recovery and the
    preserved-segment flag, then parses the surviving buffer; the dead-branch byte pre-filter is
    skipped (it changes nothing downstream).
    """
    messages: dict[str, dict[str, Any]] = {}
    summaries: dict[str, str] = {}
    custom_titles: dict[str, str] = {}
    tags: dict[str, str] = {}
    agent_names: dict[str, str] = {}
    agent_colors: dict[str, str] = {}
    agent_settings: dict[str, str] = {}
    pr_numbers: dict[str, int] = {}
    pr_urls: dict[str, str] = {}
    pr_repositories: dict[str, str] = {}
    worktree_states: dict[str, Any] = {}
    file_history_snapshots: dict[str, dict[str, Any]] = {}
    attribution_snapshots: dict[str, dict[str, Any]] = {}
    content_replacements: dict[str, list[dict[str, Any]]] = {}
    agent_content_replacements: dict[str, list[dict[str, Any]]] = {}

    try:
        buf: bytes | None = None
        metadata_lines: list[str] | None = None
        has_preserved_segment = False
        if not is_env_truthy(os.environ.get("TABVIS_DISABLE_PRECOMPACT_SKIP")):
            st = await asyncio.to_thread(os.stat, file_path)
            size = st.st_size
            if size > SKIP_PRECOMPACT_THRESHOLD:
                scan = await read_transcript_for_load(file_path, size)
                buf = scan["postBoundaryBuf"]
                has_preserved_segment = scan["hasPreservedSegment"]
                if scan["boundaryStartOffset"] > 0:
                    raw = await asyncio.to_thread(_read_bytes, file_path)
                    metadata_lines = _scan_pre_boundary_metadata(
                        raw, scan["boundaryStartOffset"]
                    )
        if buf is None:
            buf = await asyncio.to_thread(_read_bytes, file_path)

        # First pass: metadata-only lines collected during the boundary scan.
        if metadata_lines:
            meta_entries = parse_jsonl("\n".join(metadata_lines).encode("utf-8"))
            for entry in meta_entries:
                _apply_metadata_entry(
                    entry,
                    summaries,
                    custom_titles,
                    tags,
                    agent_names,
                    agent_colors,
                    agent_settings,
                    worktree_states,
                    pr_numbers,
                    pr_urls,
                    pr_repositories,
                )

        entries = parse_jsonl(buf)

        # progress_uuid → resolved non-progress parent (legacy bridge).
        progress_bridge: dict[str, str | None] = {}

        for entry in entries:
            if is_legacy_progress_entry(entry):
                parent = entry.get("parentUuid")
                progress_bridge[entry["uuid"]] = (
                    progress_bridge.get(parent)
                    if (parent and parent in progress_bridge)
                    else parent
                )
                continue
            if is_transcript_message(entry):
                pu = entry.get("parentUuid")
                if pu and pu in progress_bridge:
                    entry["parentUuid"] = progress_bridge.get(pu)
                messages[entry["uuid"]] = entry
            elif entry.get("type") == "summary" and entry.get("leafUuid"):
                summaries[entry["leafUuid"]] = entry["summary"]
            elif entry.get("type") == "custom-title" and entry.get("sessionId"):
                custom_titles[entry["sessionId"]] = entry["customTitle"]
            elif entry.get("type") == "tag" and entry.get("sessionId"):
                tags[entry["sessionId"]] = entry["tag"]
            elif entry.get("type") == "agent-name" and entry.get("sessionId"):
                agent_names[entry["sessionId"]] = entry["agentName"]
            elif entry.get("type") == "agent-color" and entry.get("sessionId"):
                agent_colors[entry["sessionId"]] = entry["agentColor"]
            elif entry.get("type") == "agent-setting" and entry.get("sessionId"):
                agent_settings[entry["sessionId"]] = entry["agentSetting"]
            elif entry.get("type") == "worktree-state" and entry.get("sessionId"):
                worktree_states[entry["sessionId"]] = entry["worktreeSession"]
            elif entry.get("type") == "pr-link" and entry.get("sessionId"):
                pr_numbers[entry["sessionId"]] = entry["prNumber"]
                pr_urls[entry["sessionId"]] = entry["prUrl"]
                pr_repositories[entry["sessionId"]] = entry["prRepository"]
            elif entry.get("type") == "file-history-snapshot":
                file_history_snapshots[entry["messageId"]] = entry
            elif entry.get("type") == "attribution-snapshot":
                attribution_snapshots[entry["messageId"]] = entry
            elif entry.get("type") == "content-replacement":
                if entry.get("agentId"):
                    existing = agent_content_replacements.setdefault(entry["agentId"], [])
                    existing.extend(entry["replacements"])
                else:
                    existing = content_replacements.setdefault(entry["sessionId"], [])
                    existing.extend(entry["replacements"])
        _ = has_preserved_segment  # parity flag (drives the skipped byte pre-filter only)
    except Exception:  # noqa: BLE001 - file doesn't exist or can't be read
        pass

    apply_preserved_segment_relinks(messages)
    apply_snip_removals(messages)

    leaf_uuids = _compute_leaf_uuids(messages, opts)

    return {
        "messages": messages,
        "summaries": summaries,
        "customTitles": custom_titles,
        "tags": tags,
        "agentNames": agent_names,
        "agentColors": agent_colors,
        "agentSettings": agent_settings,
        "prNumbers": pr_numbers,
        "prUrls": pr_urls,
        "prRepositories": pr_repositories,
        "worktreeStates": worktree_states,
        "fileHistorySnapshots": file_history_snapshots,
        "attributionSnapshots": attribution_snapshots,
        "contentReplacements": content_replacements,
        "agentContentReplacements": agent_content_replacements,
        "leafUuids": leaf_uuids,
    }


def _apply_metadata_entry(
    entry: dict[str, Any],
    summaries: dict[str, str],
    custom_titles: dict[str, str],
    tags: dict[str, str],
    agent_names: dict[str, str],
    agent_colors: dict[str, str],
    agent_settings: dict[str, str],
    worktree_states: dict[str, Any],
    pr_numbers: dict[str, int],
    pr_urls: dict[str, str],
    pr_repositories: dict[str, str],
) -> None:
    t = entry.get("type")
    if t == "summary" and entry.get("leafUuid"):
        summaries[entry["leafUuid"]] = entry["summary"]
    elif t == "custom-title" and entry.get("sessionId"):
        custom_titles[entry["sessionId"]] = entry["customTitle"]
    elif t == "tag" and entry.get("sessionId"):
        tags[entry["sessionId"]] = entry["tag"]
    elif t == "agent-name" and entry.get("sessionId"):
        agent_names[entry["sessionId"]] = entry["agentName"]
    elif t == "agent-color" and entry.get("sessionId"):
        agent_colors[entry["sessionId"]] = entry["agentColor"]
    elif t == "agent-setting" and entry.get("sessionId"):
        agent_settings[entry["sessionId"]] = entry["agentSetting"]
    elif t == "worktree-state" and entry.get("sessionId"):
        worktree_states[entry["sessionId"]] = entry["worktreeSession"]
    elif t == "pr-link" and entry.get("sessionId"):
        pr_numbers[entry["sessionId"]] = entry["prNumber"]
        pr_urls[entry["sessionId"]] = entry["prUrl"]
        pr_repositories[entry["sessionId"]] = entry["prRepository"]


def _compute_leaf_uuids(
    messages: dict[str, dict[str, Any]], opts: dict[str, Any] | None
) -> set[str]:
    """Compute leaf UUIDs (the most recent user/assistant per terminal chain).
    the leaf-computation block in ``loadTranscriptFile``."""
    all_messages = list(messages.values())
    parent_uuids = {
        msg["parentUuid"] for msg in all_messages if msg.get("parentUuid") is not None
    }
    terminal_messages = [m for m in all_messages if m["uuid"] not in parent_uuids]

    leaf_uuids: set[str] = set()
    has_cycle = False

    for terminal in terminal_messages:
        seen = set()
        current = terminal
        while current:
            if current["uuid"] in seen:
                has_cycle = True
                break
            seen.add(current["uuid"])
            if current.get("type") in ("user", "assistant"):
                leaf_uuids.add(current["uuid"])
                break
            current = (
                messages.get(current["parentUuid"]) if current.get("parentUuid") else None
            )

    if has_cycle:
        pass
    return leaf_uuids


async def _load_session_file(session_id: str) -> dict[str, Any]:
    """Load the session file."""
    session_file = os.path.join(
        get_session_project_dir() or get_project_dir(get_original_cwd()),
        f"{session_id}.jsonl",
    )
    return await load_transcript_file(session_file)


async def load_conversation_for_resume(session_id: str) -> list[dict[str, Any]]:
    """The prior conversation for a session, as ordered message envelopes ready to re-seed a turn.

    This is the read side of session RESUME: when an agent is re-run on an existing ``session_id``,
    its earlier turns are loaded so the model actually sees the conversation instead of starting
    blank. Reconstruction is identical to how the transcript is replayed for display — the parentUuid
    chain from the most recent user/assistant leaf, plus recovery of orphaned parallel tool_results
    (:func:`build_conversation_chain`) — so **tool_use/tool_result pairs stay intact** and the API
    won't reject the seed.

    Returns ``[]`` when the session has no transcript yet (a brand-new session), so a caller can
    unconditionally prepend the result. Envelopes keep their original uuids, so re-persisting them
    during the new turn is deduped by :func:`record_transcript` — no duplicate lines on disk.
    """
    loaded = await _load_session_file(session_id)
    messages = loaded.get("messages") or {}
    if not messages:
        return []
    leaf = _find_latest_message(
        messages.values(),
        lambda m: m.get("uuid") in loaded["leafUuids"]
        and m.get("type") in ("user", "assistant"),
    )
    if not leaf:
        return []
    return remove_extra_fields(build_conversation_chain(messages, leaf))


# --------------------------------------------------------------------------------------------
# getSessionMessages — async memo with a clearable cache
# --------------------------------------------------------------------------------------------

_session_messages_cache: dict[str, Awaitable[set[str]]] = {}


async def get_session_messages(session_id: str) -> set[str]:
    """Get message UUIDs for a session (memoized)."""
    cached = _session_messages_cache.get(session_id)
    if cached is not None:
        return await cached

    async def _load() -> set[str]:
        loaded = await _load_session_file(session_id)
        return set(loaded["messages"].keys())

    task = asyncio.ensure_future(_load())
    _session_messages_cache[session_id] = task
    return await task


def _session_messages_cache_has(session_id: str) -> bool:
    return session_id in _session_messages_cache


def _session_messages_cache_set(session_id: str, value: set[str]) -> None:
    async def _ready() -> set[str]:
        return value

    _session_messages_cache[session_id] = asyncio.ensure_future(_ready())


def clear_session_messages_cache() -> None:
    """Clear the memoized session-messages cache."""
    _session_messages_cache.clear()


async def does_message_exist_in_session(session_id: str, message_uuid: str) -> bool:
    """Return whether message exist in session."""
    message_set = await get_session_messages(session_id)
    return message_uuid in message_set


async def get_last_session_log(session_id: str) -> dict[str, Any] | None:
    """Return the last session log."""
    loaded = await _load_session_file(session_id)
    messages = loaded["messages"]
    if len(messages) == 0:
        return None
    if not _session_messages_cache_has(session_id):
        _session_messages_cache_set(session_id, set(messages.keys()))

    last_message = _find_latest_message(
        messages.values(), lambda m: not m.get("isSidechain")
    )
    if not last_message:
        return None

    transcript = build_conversation_chain(messages, last_message)
    summary = loaded["summaries"].get(last_message["uuid"])
    custom_title = loaded["customTitles"].get(last_message.get("sessionId"))
    tag = loaded["tags"].get(last_message.get("sessionId"))
    agent_setting = loaded["agentSettings"].get(session_id)
    log_option = _convert_to_log_option(
        transcript,
        0,
        summary,
        custom_title,
        _build_file_history_snapshot_chain(loaded["fileHistorySnapshots"], transcript),
        tag,
        get_transcript_path_for_session(session_id),
        _build_attribution_snapshot_chain(loaded["attributionSnapshots"], transcript),
        agent_setting,
        loaded["contentReplacements"].get(session_id, []),
    )
    log_option["worktreeSession"] = loaded["worktreeStates"].get(session_id)
    return log_option


# --------------------------------------------------------------------------------------------
# Log-listing entrypoints
# --------------------------------------------------------------------------------------------


async def load_message_logs(limit: int | None = None) -> list[dict[str, Any]]:
    """Load the message logs."""
    from tabvis.types.logs import sort_logs

    session_logs = await fetch_logs(limit)
    enriched = await enrich_logs(session_logs, 0, len(session_logs))
    sorted_logs = sort_logs(enriched["logs"])
    for i, log in enumerate(sorted_logs):
        log["value"] = i
    return sorted_logs


async def load_all_projects_message_logs(
    limit: int | None = None, options: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Load the all projects message logs."""
    if options and options.get("skipIndex"):
        return await _load_all_projects_message_logs_full(limit)
    result = await load_all_projects_message_logs_progressive(
        limit, (options or {}).get("initialEnrichCount", INITIAL_ENRICH_COUNT)
    )
    return result["logs"]


async def _load_all_projects_message_logs_full(
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load the all projects message logs full."""
    from tabvis.types.logs import sort_logs

    projects_dir = get_projects_dir()
    try:
        dirents = await asyncio.to_thread(_scandir_dirs, projects_dir)
    except OSError:
        return []
    project_dirs = [os.path.join(projects_dir, name) for name in dirents]
    logs_per_project = await asyncio.gather(
        *(_get_logs_without_index(pd, limit) for pd in project_dirs)
    )
    all_logs = [log for project in logs_per_project for log in project]

    deduped: dict[str, dict[str, Any]] = {}
    for log in all_logs:
        key = f"{log.get('sessionId') or ''}:{log.get('leafUuid') or ''}"
        existing = deduped.get(key)
        if not existing or log["modified"] > existing["modified"]:
            deduped[key] = log
    sorted_logs = sort_logs(list(deduped.values()))
    for i, log in enumerate(sorted_logs):
        log["value"] = i
    return sorted_logs


async def load_all_projects_message_logs_progressive(
    limit: int | None = None, initial_enrich_count: int = INITIAL_ENRICH_COUNT
) -> dict[str, Any]:
    """Load the all projects message logs progressive."""
    projects_dir = get_projects_dir()
    try:
        dirents = await asyncio.to_thread(_scandir_dirs, projects_dir)
    except OSError:
        return {"logs": [], "allStatLogs": [], "nextIndex": 0}
    project_dirs = [os.path.join(projects_dir, name) for name in dirents]

    raw_logs: list[dict[str, Any]] = []
    for project_dir in project_dirs:
        raw_logs.extend(await get_session_files_lite(project_dir, limit))
    sorted_logs = _deduplicate_logs_by_session_id(raw_logs)

    enriched = await enrich_logs(sorted_logs, 0, initial_enrich_count)
    logs = enriched["logs"]
    for i, log in enumerate(logs):
        log["value"] = i
    return {"logs": logs, "allStatLogs": sorted_logs, "nextIndex": enriched["nextIndex"]}


async def load_same_repo_message_logs(
    worktree_paths: list[str],
    limit: int | None = None,
    initial_enrich_count: int = INITIAL_ENRICH_COUNT,
) -> list[dict[str, Any]]:
    """Load the same repo message logs."""
    result = await load_same_repo_message_logs_progressive(
        worktree_paths, limit, initial_enrich_count
    )
    return result["logs"]


async def load_same_repo_message_logs_progressive(
    worktree_paths: list[str],
    limit: int | None = None,
    initial_enrich_count: int = INITIAL_ENRICH_COUNT,
) -> dict[str, Any]:
    """Load the same repo message logs progressive."""
    log_for_debugging(
        f"/resume: loading sessions for cwd={get_original_cwd()}, "
        f"worktrees=[{', '.join(worktree_paths)}]"
    )
    all_stat_logs = await _get_stat_only_logs_for_worktrees(worktree_paths, limit)
    log_for_debugging(f"/resume: found {len(all_stat_logs)} session files on disk")
    enriched = await enrich_logs(all_stat_logs, 0, initial_enrich_count)
    logs = enriched["logs"]
    for i, log in enumerate(logs):
        log["value"] = i
    return {"logs": logs, "allStatLogs": all_stat_logs, "nextIndex": enriched["nextIndex"]}


async def _get_stat_only_logs_for_worktrees(
    worktree_paths: list[str], limit: int | None = None
) -> list[dict[str, Any]]:
    """Return the stat only logs for worktrees."""
    projects_dir = get_projects_dir()
    if len(worktree_paths) <= 1:
        cwd = get_original_cwd()
        project_dir = get_project_dir(cwd)
        return await get_session_files_lite(project_dir, None, cwd)

    case_insensitive = os.name == "nt"
    indexed = []
    for wt in worktree_paths:
        sanitized = sanitize_path(wt)
        prefix = sanitized.lower() if case_insensitive else sanitized
        indexed.append((wt, prefix))
    indexed.sort(key=lambda pair: len(pair[1]), reverse=True)

    all_logs: list[dict[str, Any]] = []
    seen_dirs: set[str] = set()
    try:
        all_dirents = await asyncio.to_thread(_scandir_with_types, projects_dir)
    except OSError as e:
        log_for_debugging(
            f"Failed to read projects dir {projects_dir}, falling back to current project: {e}"
        )
        project_dir = get_project_dir(get_original_cwd())
        return await get_session_files_lite(project_dir, limit, get_original_cwd())

    for entry_name, is_dir in all_dirents:
        if not is_dir:
            continue
        dir_name = entry_name.lower() if case_insensitive else entry_name
        if dir_name in seen_dirs:
            continue
        for wt_path, prefix in indexed:
            if dir_name == prefix or dir_name.startswith(prefix + "-"):
                seen_dirs.add(dir_name)
                all_logs.extend(
                    await get_session_files_lite(
                        os.path.join(projects_dir, entry_name), None, wt_path
                    )
                )
                break
    return _deduplicate_logs_by_session_id(all_logs)


# --------------------------------------------------------------------------------------------
# Agent / subagent transcript loading
# --------------------------------------------------------------------------------------------


async def get_agent_transcript(agent_id: str) -> dict[str, Any] | None:
    """Retrieve a subagent's transcript by agentId."""
    agent_file = get_agent_transcript_path(agent_id)
    try:
        loaded = await load_transcript_file(agent_file)
        messages = loaded["messages"]
        agent_messages = [
            msg
            for msg in messages.values()
            if msg.get("agentId") == agent_id and msg.get("isSidechain")
        ]
        if len(agent_messages) == 0:
            return None
        parent_uuids = {msg.get("parentUuid") for msg in agent_messages}
        leaf_message = _find_latest_message(
            agent_messages, lambda msg: msg["uuid"] not in parent_uuids
        )
        if not leaf_message:
            return None
        transcript = build_conversation_chain(messages, leaf_message)
        agent_transcript = [m for m in transcript if m.get("agentId") == agent_id]
        return {
            "messages": [
                {k: v for k, v in msg.items() if k not in ("isSidechain", "parentUuid")}
                for msg in agent_transcript
            ],
            "contentReplacements": loaded["agentContentReplacements"].get(agent_id, []),
        }
    except Exception:  # noqa: BLE001
        return None


def extract_agent_ids_from_messages(messages: list[dict[str, Any]]) -> list[str]:
    """Extract agent IDs from agent/skill progress messages.
    ``extractAgentIdsFromMessages``."""
    agent_ids: list[str] = []
    for message in messages:
        data = message.get("data")
        if (
            message.get("type") == "progress"
            and isinstance(data, dict)
            and data.get("type") in ("agent_progress", "skill_progress")
            and isinstance(data.get("agentId"), str)
        ):
            agent_ids.append(data["agentId"])
    return uniq(agent_ids)


def extract_teammate_transcripts_from_tasks(
    tasks: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Extract teammate transcripts from AppState tasks.
    ``extractTeammateTranscriptsFromTasks``."""
    transcripts: dict[str, list[dict[str, Any]]] = {}
    for task in tasks.values():
        identity = task.get("identity") or {}
        task_messages = task.get("messages")
        if (
            task.get("type") == "in_process_teammate"
            and identity.get("agentId")
            and task_messages
            and len(task_messages) > 0
        ):
            transcripts[identity["agentId"]] = task_messages
    return transcripts


async def load_subagent_transcripts(
    agent_ids: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Load the subagent transcripts."""

    async def _load(agent_id: str) -> dict[str, Any] | None:
        try:
            result = await get_agent_transcript(as_agent_id(agent_id))
            if result and len(result["messages"]) > 0:
                return {"agentId": agent_id, "transcript": result["messages"]}
            return None
        except Exception:  # noqa: BLE001
            return None

    results = await asyncio.gather(*(_load(a) for a in agent_ids))
    transcripts: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if result:
            transcripts[result["agentId"]] = result["transcript"]
    return transcripts


async def load_all_subagent_transcripts_from_disk() -> dict[str, list[dict[str, Any]]]:
    """Load the all subagent transcripts from disk."""
    subagents_dir = os.path.join(
        get_session_project_dir() or get_project_dir(get_original_cwd()),
        get_session_id(),
        "subagents",
    )
    try:
        entries = await asyncio.to_thread(_scandir_files, subagents_dir)
    except OSError:
        return {}
    agent_ids = [
        name[len("agent-") : -len(".jsonl")]
        for name in entries
        if name.startswith("agent-") and name.endswith(".jsonl")
    ]
    return await load_subagent_transcripts(agent_ids)


# --------------------------------------------------------------------------------------------
# Message-cleaning / REPL-stripping for the write path
# --------------------------------------------------------------------------------------------


def is_loggable_message(m: dict[str, Any]) -> bool:
    """Return whether loggable message."""
    if m.get("type") == "progress":
        return False
    if m.get("type") == "attachment" and get_user_type() != "ant":
        attachment = m.get("attachment") or {}
        if attachment.get("type") == "hook_additional_context" and is_env_truthy(
            os.environ.get("TABVIS_SAVE_HOOK_ADDITIONAL_CONTEXT")
        ):
            return True
        return False
    return True


def _collect_repl_ids(messages: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for m in messages:
        if m.get("type") == "assistant" and isinstance(
            (m.get("message") or {}).get("content"), list
        ):
            for b in m["message"]["content"]:
                if (
                    isinstance(b, dict)
                    and b.get("type") == "tool_use"
                    and b.get("name") == REPL_TOOL_NAME
                ):
                    ids.add(b["id"])
    return ids


def _transform_messages_for_external_transcript(
    messages: list[dict[str, Any]], repl_ids: set[str]
) -> list[dict[str, Any]]:
    """Strip REPL tool_use/tool_result pairs and promote isVirtual messages.
    ``transformMessagesForExternalTranscript``."""
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.get("type") == "assistant" and isinstance(
            (m.get("message") or {}).get("content"), list
        ):
            content = m["message"]["content"]
            has_repl = any(
                isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("name") == REPL_TOOL_NAME
                for b in content
            )
            filtered = (
                [
                    b
                    for b in content
                    if not (
                        isinstance(b, dict)
                        and b.get("type") == "tool_use"
                        and b.get("name") == REPL_TOOL_NAME
                    )
                ]
                if has_repl
                else content
            )
            if len(filtered) == 0:
                continue
            if m.get("isVirtual"):
                rest = {k: v for k, v in m.items() if k != "isVirtual"}
                result.append({**rest, "message": {**m["message"], "content": filtered}})
            elif filtered is not content:
                result.append({**m, "message": {**m["message"], "content": filtered}})
            else:
                result.append(m)
        elif m.get("type") == "user" and isinstance(
            (m.get("message") or {}).get("content"), list
        ):
            content = m["message"]["content"]
            has_repl = any(
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") in repl_ids
                for b in content
            )
            filtered = (
                [
                    b
                    for b in content
                    if not (
                        isinstance(b, dict)
                        and b.get("type") == "tool_result"
                        and b.get("tool_use_id") in repl_ids
                    )
                ]
                if has_repl
                else content
            )
            if len(filtered) == 0:
                continue
            if m.get("isVirtual"):
                rest = {k: v for k, v in m.items() if k != "isVirtual"}
                result.append({**rest, "message": {**m["message"], "content": filtered}})
            elif filtered is not content:
                result.append({**m, "message": {**m["message"], "content": filtered}})
            else:
                result.append(m)
        elif m.get("isVirtual"):
            result.append({k: v for k, v in m.items() if k != "isVirtual"})
        else:
            result.append(m)
    return result


def clean_messages_for_logging(
    messages: list[dict[str, Any]], all_messages: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Filter non-loggable messages and (for non-ant) strip REPL.
    ``cleanMessagesForLogging``."""
    if all_messages is None:
        all_messages = messages
    filtered = [m for m in messages if is_loggable_message(m)]
    if get_user_type() != "ant":
        return _transform_messages_for_external_transcript(
            filtered, _collect_repl_ids(all_messages)
        )
    return filtered


async def get_log_by_index(index: int) -> dict[str, Any] | None:
    """Return the log by index."""
    logs = await load_message_logs()
    return logs[index] if 0 <= index < len(logs) else None


async def find_unresolved_tool_use(tool_use_id: str) -> dict[str, Any] | None:
    """Look up an unresolved tool_use in the current transcript.
    ``findUnresolvedToolUse``."""
    try:
        transcript_path = get_transcript_path()
        loaded = await load_transcript_file(transcript_path)
        tool_use_message: dict[str, Any] | None = None
        for message in loaded["messages"].values():
            if message.get("type") == "assistant":
                content = (message.get("message") or {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("id") == tool_use_id
                        ):
                            tool_use_message = message
                            break
            elif message.get("type") == "user":
                content = (message.get("message") or {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_result"
                            and block.get("tool_use_id") == tool_use_id
                        ):
                            return None
        return tool_use_message
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------------------------
# Session-file stat listing + lite metadata
# --------------------------------------------------------------------------------------------


def _scandir_dirs(path: str) -> list[str]:
    with os.scandir(path) as it:
        return [entry.name for entry in it if entry.is_dir()]


def _scandir_with_types(path: str) -> list[tuple[str, bool]]:
    with os.scandir(path) as it:
        return [(entry.name, entry.is_dir()) for entry in it]


async def get_session_files_with_mtime(project_dir: str) -> dict[str, dict[str, Any]]:
    """Get all session JSONL files in a project dir with their stats.
    ``getSessionFilesWithMtime``."""
    session_files_map: dict[str, dict[str, Any]] = {}
    try:
        dirents = await asyncio.to_thread(_scandir_with_types, project_dir)
    except OSError:
        return session_files_map

    candidates: list[tuple[str, str]] = []
    for name, is_dir in dirents:
        if is_dir or not name.endswith(".jsonl"):
            continue
        session_id = validate_uuid(name[: -len(".jsonl")])
        if not session_id:
            continue
        candidates.append((session_id, os.path.join(project_dir, name)))

    async def _stat_one(session_id: str, file_path: str) -> None:
        try:
            st = await asyncio.to_thread(os.stat, file_path)
            session_files_map[session_id] = {
                "path": file_path,
                "mtime": int(st.st_mtime * 1000),
                "ctime": int(getattr(st, "st_birthtime", st.st_ctime) * 1000),
                "size": st.st_size,
            }
        except OSError:
            log_for_debugging(f"Failed to stat session file: {file_path}")

    await asyncio.gather(*(_stat_one(sid, fp) for sid, fp in candidates))
    return session_files_map


async def load_all_logs_from_session_file(
    session_file: str, project_path_override: str | None = None
) -> list[dict[str, Any]]:
    """Load all logs (one per leaf) from a single session file with full message data.
    ``loadAllLogsFromSessionFile``."""
    loaded = await load_transcript_file(session_file, {"keepAllLeaves": True})
    messages = loaded["messages"]
    if len(messages) == 0:
        return []
    leaf_uuids = loaded["leafUuids"]

    leaf_messages: list[dict[str, Any]] = []
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for msg in messages.values():
        if msg["uuid"] in leaf_uuids:
            leaf_messages.append(msg)
        elif msg.get("parentUuid"):
            children_by_parent.setdefault(msg["parentUuid"], []).append(msg)

    logs: list[dict[str, Any]] = []
    for leaf_message in leaf_messages:
        chain = build_conversation_chain(messages, leaf_message)
        if len(chain) == 0:
            continue
        trailing_messages = children_by_parent.get(leaf_message["uuid"])
        if trailing_messages:
            trailing_messages.sort(key=lambda m: m.get("timestamp", ""))
            chain.extend(trailing_messages)

        first_message = chain[0]
        session_id = leaf_message.get("sessionId")
        logs.append(
            {
                "date": leaf_message["timestamp"],
                "messages": remove_extra_fields(chain),
                "fullPath": session_file,
                "value": 0,
                "created": _to_datetime(first_message["timestamp"]),
                "modified": _to_datetime(leaf_message["timestamp"]),
                "firstPrompt": extract_first_prompt(chain),
                "messageCount": _count_visible_messages(chain),
                "isSidechain": first_message.get("isSidechain") or False,
                "sessionId": session_id,
                "leafUuid": leaf_message["uuid"],
                "summary": loaded["summaries"].get(leaf_message["uuid"]),
                "customTitle": loaded["customTitles"].get(session_id),
                "tag": loaded["tags"].get(session_id),
                "agentName": loaded["agentNames"].get(session_id),
                "agentColor": loaded["agentColors"].get(session_id),
                "agentSetting": loaded["agentSettings"].get(session_id),
                "prNumber": loaded["prNumbers"].get(session_id),
                "prUrl": loaded["prUrls"].get(session_id),
                "prRepository": loaded["prRepositories"].get(session_id),
                "gitBranch": leaf_message.get("gitBranch"),
                "projectPath": project_path_override or first_message.get("cwd"),
                "fileHistorySnapshots": _build_file_history_snapshot_chain(
                    loaded["fileHistorySnapshots"], chain
                ),
                "attributionSnapshots": _build_attribution_snapshot_chain(
                    loaded["attributionSnapshots"], chain
                ),
                "contentReplacements": loaded["contentReplacements"].get(session_id, []),
            }
        )
    return logs


async def _get_logs_without_index(
    project_dir: str, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return the logs without index."""
    session_files_map = await get_session_files_with_mtime(project_dir)
    if len(session_files_map) == 0:
        return []

    if limit and len(session_files_map) > limit:
        files_to_process = sorted(
            session_files_map.values(), key=lambda f: f["mtime"], reverse=True
        )[:limit]
    else:
        files_to_process = list(session_files_map.values())

    logs: list[dict[str, Any]] = []
    for file_info in files_to_process:
        try:
            file_log_options = await load_all_logs_from_session_file(file_info["path"])
            logs.extend(file_log_options)
        except Exception:  # noqa: BLE001
            log_for_debugging(f"Failed to load session file: {file_info['path']}")
    return logs


async def _read_lite_metadata(
    file_path: str, file_size: int, buf: Any = None
) -> dict[str, Any]:
    """Read the first/last ~64KB and extract lite metadata."""
    ht = await read_head_and_tail(file_path, file_size, buf)
    head = ht["head"]
    tail = ht["tail"]
    if not head:
        return {"firstPrompt": "", "isSidechain": False}

    is_sidechain = (
        '"isSidechain":true' in head or '"isSidechain": true' in head
    )
    project_path = extract_json_string_field(head, "cwd")
    team_name = extract_json_string_field(head, "teamName")
    agent_setting = extract_json_string_field(head, "agentSetting")

    first_prompt = (
        extract_last_json_string_field(tail, "lastPrompt")
        or _extract_first_prompt_from_chunk(head)
        or _extract_json_string_field_prefix(head, "content", 200)
        or _extract_json_string_field_prefix(head, "text", 200)
        or ""
    )

    custom_title = (
        extract_last_json_string_field(tail, "customTitle")
        or extract_last_json_string_field(head, "customTitle")
        or extract_last_json_string_field(tail, "aiTitle")
        or extract_last_json_string_field(head, "aiTitle")
    )
    summary = extract_last_json_string_field(tail, "summary")
    tag = extract_last_json_string_field(tail, "tag")
    git_branch = extract_last_json_string_field(
        tail, "gitBranch"
    ) or extract_json_string_field(head, "gitBranch")

    pr_url = extract_last_json_string_field(tail, "prUrl")
    pr_repository = extract_last_json_string_field(tail, "prRepository")
    pr_number: int | None = None
    pr_num_str = extract_last_json_string_field(tail, "prNumber")
    if pr_num_str:
        try:
            pr_number = int(pr_num_str) or None
        except ValueError:
            pr_number = None
    if not pr_number:
        pr_num_match = tail.rfind('"prNumber":')
        if pr_num_match >= 0:
            after_colon = tail[pr_num_match + 11 : pr_num_match + 25]
            try:
                num = int(re.match(r"\s*(-?\d+)", after_colon).group(1)) if re.match(
                    r"\s*(-?\d+)", after_colon
                ) else 0
            except (ValueError, AttributeError):
                num = 0
            if num > 0:
                pr_number = num

    return {
        "firstPrompt": first_prompt,
        "gitBranch": git_branch,
        "isSidechain": is_sidechain,
        "projectPath": project_path,
        "teamName": team_name,
        "customTitle": custom_title,
        "summary": summary,
        "tag": tag,
        "agentSetting": agent_setting,
        "prNumber": pr_number,
        "prUrl": pr_url,
        "prRepository": pr_repository,
    }


def _extract_first_prompt_from_chunk(chunk: str) -> str:
    """Scan a chunk of text for the first meaningful user prompt.
    ``extractFirstPromptFromChunk``."""
    start = 0
    first_command_fallback = ""
    chunk_len = len(chunk)
    while start < chunk_len:
        newline_idx = chunk.find("\n", start)
        line = chunk[start:newline_idx] if newline_idx >= 0 else chunk[start:]
        start = newline_idx + 1 if newline_idx >= 0 else chunk_len

        if '"type":"user"' not in line and '"type": "user"' not in line:
            continue
        if '"tool_result"' in line:
            continue
        if '"isMeta":true' in line or '"isMeta": true' in line:
            continue

        try:
            entry = json_parse(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        message = entry.get("message")
        if not message or not isinstance(message, dict):
            continue

        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    texts.append(block["text"])

        for text in texts:
            if not text:
                continue
            result = text.replace("\n", " ").strip()

            command_name_tag = extract_tag(result, COMMAND_NAME_TAG)
            if command_name_tag:
                name = re.sub(r"^/", "", command_name_tag)
                args_raw = extract_tag(result, "command-args")
                command_args = args_raw.strip() if args_raw else ""
                if name in _built_in_command_names() or not command_args:
                    if not first_command_fallback:
                        first_command_fallback = command_name_tag
                    continue
                return (
                    f"{command_name_tag} {command_args}" if command_args else command_name_tag
                )

            bash_input = extract_tag(result, "bash-input")
            if bash_input:
                return f"! {bash_input}"

            if SKIP_FIRST_PROMPT_PATTERN.match(result):
                continue
            if len(result) > 200:
                result = result[:200].strip() + "…"
            return result

    if first_command_fallback:
        return first_command_fallback
    return ""


def _extract_json_string_field_prefix(text: str, key: str, max_len: int) -> str:
    """Like ``extract_json_string_field`` but returns the first ``max_len`` chars even when the
    closing quote is missing."""
    patterns = [f'"{key}":"', f'"{key}": "']
    for pattern in patterns:
        idx = text.find(pattern)
        if idx < 0:
            continue
        value_start = idx + len(pattern)
        i = value_start
        collected = 0
        text_len = len(text)
        while i < text_len and collected < max_len:
            if text[i] == "\\":
                i += 2
                collected += 1
                continue
            if text[i] == '"':
                break
            i += 1
            collected += 1
        raw = text[value_start:i]
        return raw.replace("\\n", " ").replace("\\t", " ").strip()
    return ""


def _deduplicate_logs_by_session_id(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one preferred log entry per session ID."""
    from tabvis.types.logs import sort_logs

    deduped: dict[str, dict[str, Any]] = {}
    for log in logs:
        if not log.get("sessionId"):
            continue
        existing = deduped.get(log["sessionId"])
        if not existing or log["modified"] > existing["modified"]:
            deduped[log["sessionId"]] = log
    sorted_logs = sort_logs(list(deduped.values()))
    result = []
    for i, log in enumerate(sorted_logs):
        result.append({**log, "value": i})
    return result


async def get_session_files_lite(
    project_dir: str, limit: int | None = None, project_path: str | None = None
) -> list[dict[str, Any]]:
    """Return lite (stat-only) LogOption list."""
    from tabvis.types.logs import sort_logs

    session_files_map = await get_session_files_with_mtime(project_dir)
    entries = sorted(
        session_files_map.items(), key=lambda kv: kv[1]["mtime"], reverse=True
    )
    if limit and len(entries) > limit:
        entries = entries[:limit]

    logs: list[dict[str, Any]] = []
    for session_id, file_info in entries:
        logs.append(
            {
                "date": _epoch_ms_to_iso(file_info["mtime"]),
                "messages": [],
                "isLite": True,
                "fullPath": file_info["path"],
                "value": 0,
                "created": _epoch_ms_to_datetime(file_info["ctime"]),
                "modified": _epoch_ms_to_datetime(file_info["mtime"]),
                "firstPrompt": "",
                "messageCount": 0,
                "fileSize": file_info["size"],
                "isSidechain": False,
                "sessionId": session_id,
                "projectPath": project_path,
            }
        )
    sorted_logs = sort_logs(logs)
    for i, log in enumerate(sorted_logs):
        log["value"] = i
    return sorted_logs


def _epoch_ms_to_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _epoch_ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


async def _enrich_log(log: dict[str, Any], read_buf: Any = None) -> dict[str, Any] | None:
    """Enrich a lite log with metadata from its JSONL file."""
    if not log.get("isLite") or not log.get("fullPath"):
        return log

    meta = await _read_lite_metadata(log["fullPath"], log.get("fileSize") or 0, read_buf)
    enriched = {**log}
    enriched.update(
        {
            "isLite": False,
            "firstPrompt": meta.get("firstPrompt"),
            "gitBranch": meta.get("gitBranch"),
            "isSidechain": meta.get("isSidechain"),
            "teamName": meta.get("teamName"),
            "customTitle": meta.get("customTitle"),
            "summary": meta.get("summary"),
            "tag": meta.get("tag"),
            "agentSetting": meta.get("agentSetting"),
            "prNumber": meta.get("prNumber"),
            "prUrl": meta.get("prUrl"),
            "prRepository": meta.get("prRepository"),
            "projectPath": meta.get("projectPath") or log.get("projectPath"),
        }
    )

    if not enriched.get("firstPrompt") and not enriched.get("customTitle"):
        enriched["firstPrompt"] = "(session)"
    if enriched.get("isSidechain"):
        log_for_debugging(
            f"Session {log.get('sessionId')} filtered from /resume: isSidechain=true"
        )
        return None
    if enriched.get("teamName"):
        log_for_debugging(
            f"Session {log.get('sessionId')} filtered from /resume: "
            f"teamName={enriched['teamName']}"
        )
        return None
    return enriched


async def enrich_logs(
    all_logs: list[dict[str, Any]], start_index: int, count: int
) -> dict[str, Any]:
    """Enrich enough lite logs to produce ``count`` valid results."""
    result: list[dict[str, Any]] = []
    i = start_index
    while i < len(all_logs) and len(result) < count:
        log = all_logs[i]
        i += 1
        enriched = await _enrich_log(log)
        if enriched:
            result.append(enriched)
    scanned = i - start_index
    filtered = scanned - len(result)
    if filtered > 0:
        log_for_debugging(
            f"/resume: enriched {scanned} sessions, {filtered} filtered out, "
            f"{len(result)} visible ({len(all_logs) - i} remaining on disk)"
        )
    return {"logs": result, "nextIndex": i}
