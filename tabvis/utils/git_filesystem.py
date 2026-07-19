"""Filesystem-based git state reading

Avoids spawning git subprocesses by reading ``.git`` directly. Covers: resolving ``.git``
directories (including worktrees/submodules), parsing HEAD, resolving refs via loose files
and ``packed-refs``, and a ``GitFileWatcher`` that caches branch/SHA and invalidates on file
change.

Correctness notes (verified against git source, carried verbatim from the TS):
  - HEAD: ``ref: refs/heads/<branch>\\n`` or raw SHA (refs/files-backend.c)
  - Packed-refs: ``<sha> <refname>\\n``, skip ``#`` and ``^`` lines (packed-backend.c)
  - ``.git`` file (worktree): ``gitdir: <path>\\n`` with optional relative path (setup.c)
  - Shallow: mere existence of ``<commonDir>/shallow`` means shallow (shallow.c)

Flat-module layout: git utilities live in flat ``tabvis/utils/git_*.py`` siblings (not a
``tabvis/utils/git/`` package) — a ``tabvis/utils/git/`` dir would SHADOW the existing
``tabvis/utils/git.py`` module (CPython resolves ``tabvis.utils.git`` to the package over the
module, breaking its importers). See memory ``tabvis-flat-tool-modules``.

Watcher fidelity: Node's ``fs.watchFile({interval})`` is a *polling* stat watcher. Python's
stdlib has no equivalent, so the watcher is implemented as an asyncio mtime-polling loop with the
same interval semantics. When no event loop is running (sync caller / clean env) the watcher
is a no-op — the cache is still correct (it just doesn't auto-invalidate). ``reset()`` clears
everything. ``register_cleanup`` wires teardown into graceful shutdown.

This module also re-exports the thin ``get_branch``/``get_default_branch``/``get_head``/
``get_remote_url`` resolvers and the memoized ``git_exe`` so the ``session_storage``/
``get_worktree_paths``/``gh_pr_status`` fallbacks can import them from a single git surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import os.path as _osp
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from tabvis.bootstrap.state import wait_for_scroll_idle
from tabvis.utils.cleanup_registry import register_cleanup
from tabvis.utils.cwd import get_cwd
from tabvis.utils.git import find_git_root
from tabvis.utils.git_config_parser import parse_git_config_value
from tabvis.utils.memoize import memoize_with_lru
from tabvis.utils.which import which_sync

# ---------------------------------------------------------------------------
# resolve_git_dir — find the actual .git directory
# ---------------------------------------------------------------------------


def _safe_mtime(path: str) -> float | None:
    """Return the mtime of ``path`` (seconds), or ``None`` if it is inaccessible.

    Used by the polling fallback watcher to detect changes to .git/HEAD, config, and the
    current-branch ref file without raising on a missing/unreadable path.
    """
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


_resolve_git_dir_cache: dict[str, str | None] = {}


def clear_resolve_git_dir_cache() -> None:
    """Clear cached git dir resolutions. Exported for testing only."""
    _resolve_git_dir_cache.clear()


async def resolve_git_dir(start_path: str | None = None) -> str | None:
    """Resolve the actual ``.git`` directory for a repo.

    Handles worktrees/submodules where ``.git`` is a file containing ``gitdir: <path>``.
    Memoized per ``start_path``.
    """
    cwd = _osp.abspath(start_path if start_path is not None else get_cwd())
    if cwd in _resolve_git_dir_cache:
        return _resolve_git_dir_cache[cwd]

    root = find_git_root(cwd)
    if not root:
        _resolve_git_dir_cache[cwd] = None
        return None

    git_path = _osp.join(root, ".git")
    try:
        if _osp.isfile(git_path):
            # Worktree or submodule: .git is a file with `gitdir: <path>`.
            # Git strips trailing \n and \r (setup.c read_gitfile_gently).
            with open(git_path, encoding="utf-8") as fh:
                content = fh.read().strip()
            if content.startswith("gitdir:"):
                raw_dir = content[len("gitdir:") :].strip()
                resolved = _osp.abspath(_osp.join(root, raw_dir))
                _resolve_git_dir_cache[cwd] = resolved
                return resolved
        # Regular repo: .git is a directory.
        _resolve_git_dir_cache[cwd] = git_path
        return git_path
    except OSError:
        _resolve_git_dir_cache[cwd] = None
        return None


# ---------------------------------------------------------------------------
# is_safe_ref_name — validate ref/branch names read from .git/
# ---------------------------------------------------------------------------

_SAFE_REF_RE = re.compile(r"^[a-zA-Z0-9/._+@-]+$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def is_safe_ref_name(name: str) -> bool:
    """Validate that a ref/branch name read from ``.git/`` is safe.

    Safe for path joins, git positional args, and shell interpolation. ``.git/HEAD`` is a
    plain text file an attacker could write without git's ``check-ref-format`` validation, so
    we allowlist ASCII alphanumerics, ``/ . _ + - @`` and reject ``..``, leading ``-``/``/``,
    and empty/single-dot path components.
    """
    if not name or name.startswith("-") or name.startswith("/"):
        return False
    if ".." in name:
        return False
    # Reject single-dot and empty path components (`.`, `foo/./bar`, `foo//bar`, `foo/`).
    if any(c in (".", "") for c in name.split("/")):
        return False
    # Allowlist-only. Rejects shell metacharacters, whitespace, NUL, non-ASCII, and `@{`.
    if not _SAFE_REF_RE.match(name):
        return False
    return True


def is_valid_git_sha(s: str) -> bool:
    """Whether ``s`` is a git SHA: 40 hex chars (SHA-1) or 64 hex chars (SHA-256).

    Git never writes abbreviated SHAs to HEAD/ref files, so only full-length hashes pass.
    """
    return bool(_SHA1_RE.match(s)) or bool(_SHA256_RE.match(s))


# ---------------------------------------------------------------------------
# read_git_head — parse .git/HEAD
# ---------------------------------------------------------------------------


class HeadBranch(TypedDict):
    type: str  # 'branch'
    name: str


class HeadDetached(TypedDict):
    type: str  # 'detached'
    sha: str


async def read_git_head(git_dir: str) -> HeadBranch | HeadDetached | None:
    """Parse ``.git/HEAD`` to determine current branch or detached SHA.

    HEAD format (refs/files-backend.c):
      - ``ref: refs/heads/<branch>\\n``  — on a branch
      - ``ref: <other-ref>\\n``          — unusual symref (e.g. during bisect)
      - ``<hex-sha>\\n``                 — detached HEAD (e.g. during rebase)
    """
    try:
        with open(_osp.join(git_dir, "HEAD"), encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        return None

    if content.startswith("ref:"):
        ref = content[len("ref:") :].strip()
        if ref.startswith("refs/heads/"):
            name = ref[len("refs/heads/") :]
            # Reject path traversal and argument injection from a tampered HEAD.
            if not is_safe_ref_name(name):
                return None
            return {"type": "branch", "name": name}
        # Unusual symref (not a local branch) — resolve to SHA.
        if not is_safe_ref_name(ref):
            return None
        sha = await resolve_ref(git_dir, ref)
        return {"type": "detached", "sha": sha} if sha else {"type": "detached", "sha": ""}
    # Raw SHA (detached HEAD). Validate against shell-metacharacter injection.
    if not is_valid_git_sha(content):
        return None
    return {"type": "detached", "sha": content}


# ---------------------------------------------------------------------------
# resolve_ref — resolve loose/packed refs to SHAs
# ---------------------------------------------------------------------------


async def resolve_ref(git_dir: str, ref: str) -> str | None:
    """Resolve a git ref (e.g. ``refs/heads/main``) to a commit SHA.

    Checks loose ref files first, then ``packed-refs``. Follows symrefs. For worktrees, refs
    live in the common gitdir (``commondir``), so fall back there.
    """
    result = await _resolve_ref_in_dir(git_dir, ref)
    if result:
        return result

    common_dir = await get_common_dir(git_dir)
    if common_dir and common_dir != git_dir:
        return await _resolve_ref_in_dir(common_dir, ref)

    return None


async def _resolve_ref_in_dir(directory: str, ref: str) -> str | None:
    # Try loose ref file.
    try:
        with open(_osp.join(directory, ref), encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        content = None

    if content is not None:
        if content.startswith("ref:"):
            target = content[len("ref:") :].strip()
            # Reject path traversal in a tampered symref chain.
            if not is_safe_ref_name(target):
                return None
            return await resolve_ref(directory, target)
        # Loose ref content should be a raw SHA.
        if not is_valid_git_sha(content):
            return None
        return content

    # Loose ref doesn't exist, try packed-refs.
    try:
        with open(_osp.join(directory, "packed-refs"), encoding="utf-8") as fh:
            packed = fh.read()
    except OSError:
        return None

    for line in packed.split("\n"):
        if line.startswith("#") or line.startswith("^"):
            continue
        space_idx = line.find(" ")
        if space_idx == -1:
            continue
        if line[space_idx + 1 :] == ref:
            sha = line[:space_idx]
            return sha if is_valid_git_sha(sha) else None

    return None


async def get_common_dir(git_dir: str) -> str | None:
    """Read the ``commondir`` file to find the shared git directory.

    In a worktree this points to the main repo's ``.git`` dir. ``None`` for a regular repo.
    """
    try:
        with open(_osp.join(git_dir, "commondir"), encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        return None
    return _osp.abspath(_osp.join(git_dir, content))


async def read_symref_text(
    git_dir: str,
    ref_path: str,
    branch_prefix: str,
) -> str | None:
    """Read a raw symref file and extract the branch name after ``branch_prefix``.

    ``None`` if the ref doesn't exist, isn't a symref, or doesn't match the prefix. Loose file
    only — ``packed-refs`` doesn't store symrefs.
    """
    try:
        with open(_osp.join(git_dir, ref_path), encoding="utf-8") as fh:
            content = fh.read().strip()
    except OSError:
        return None

    if content.startswith("ref:"):
        target = content[len("ref:") :].strip()
        if target.startswith(branch_prefix):
            name = target[len(branch_prefix) :]
            # Reject path traversal and argument injection from a tampered symref.
            if not is_safe_ref_name(name):
                return None
            return name
    return None


# ---------------------------------------------------------------------------
# GitFileWatcher — watches git files and caches derived values.
# ---------------------------------------------------------------------------

_WATCH_INTERVAL_MS = 10 if os.environ.get("NODE_ENV") == "test" else 1000


class _CacheEntry(TypedDict):
    value: Any
    dirty: bool
    compute: Callable[[], Awaitable[Any]]


class GitFileWatcher:
    """Lazily-initialized cache that invalidates on watched git-file changes.

    Watches ``.git/HEAD``, ``.git/config``, and the current branch's ref file. Watching is a
    polling loop (Node ``fs.watchFile`` is itself polling). If no event loop is running, the
    watcher degrades to a pure cache (no auto-invalidation) — still correct.
    """

    def __init__(self) -> None:
        self._git_dir: str | None = None
        self._common_dir: str | None = None
        self._initialized = False
        self._init_task: asyncio.Task[None] | None = None
        self._watched_paths: list[str] = []
        self._branch_ref_path: str | None = None
        self._cache: dict[str, _CacheEntry] = {}
        # path -> last-seen mtime (None = file absent). Drives polling-based invalidation.
        self._mtimes: dict[str, float | None] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._poll_task: asyncio.Task[None] | None = None
        self._unregister_cleanup: Callable[[], None] | None = None

    async def ensure_started(self) -> None:
        if self._initialized:
            return
        if self._init_task is not None:
            await self._init_task
            return
        self._init_task = asyncio.ensure_future(self._start())
        await self._init_task

    async def _start(self) -> None:
        self._git_dir = await resolve_git_dir()
        self._initialized = True
        if not self._git_dir:
            return

        # In a worktree, branch refs and the main config live in commonDir.
        self._common_dir = await get_common_dir(self._git_dir)

        # Watch .git/HEAD and .git/config.
        self._watch_path(
            _osp.join(self._git_dir, "HEAD"),
            lambda: self._schedule(self._on_head_changed()),
        )
        self._watch_path(
            _osp.join(self._common_dir or self._git_dir, "config"),
            self._invalidate,
        )

        # Watch the current branch's ref file for commit changes.
        await self._watch_current_branch_ref()

        self._unregister_cleanup = register_cleanup(self._async_stop_watching)
        self._ensure_poll_task()

    def _watch_path(self, path: str, callback: Callable[[], None]) -> None:
        self._watched_paths.append(path)
        self._callbacks[path] = callback
        self._mtimes[path] = _safe_mtime(path)

    async def _watch_current_branch_ref(self) -> None:
        if not self._git_dir:
            return

        head = await read_git_head(self._git_dir)
        refs_dir = self._common_dir or self._git_dir
        ref_path = (
            _osp.join(refs_dir, "refs", "heads", head["name"])
            if head is not None and head["type"] == "branch"
            else None
        )

        # Already watching this ref (or already not watching anything).
        if ref_path == self._branch_ref_path:
            return

        # Stop watching the old branch ref (branch→branch AND branch→detached).
        if self._branch_ref_path:
            self._unwatch_path(self._branch_ref_path)

        self._branch_ref_path = ref_path
        if not ref_path:
            return

        # watchFile works on nonexistent files — it fires when the file appears.
        self._watch_path(ref_path, self._invalidate)

    def _unwatch_path(self, path: str) -> None:
        self._watched_paths = [p for p in self._watched_paths if p != path]
        self._callbacks.pop(path, None)
        self._mtimes.pop(path, None)

    async def _on_head_changed(self) -> None:
        # invalidate() is cheap (marks dirty); do it first, then defer file I/O until scroll
        # settles so mid-scroll callbacks don't compete for the event loop.
        self._invalidate()
        await wait_for_scroll_idle()
        await self._watch_current_branch_ref()

    def _invalidate(self) -> None:
        for entry in self._cache.values():
            entry["dirty"] = True

    def _ensure_poll_task(self) -> None:
        if self._poll_task is not None or not self._watched_paths:
            return
        with contextlib.suppress(RuntimeError):
            self._poll_task = asyncio.ensure_future(self._poll_loop())

    async def _poll_loop(self) -> None:
        interval = _WATCH_INTERVAL_MS / 1000
        try:
            while self._initialized and self._watched_paths:
                await asyncio.sleep(interval)
                for path in list(self._watched_paths):
                    current = _safe_mtime(path)
                    if current != self._mtimes.get(path):
                        self._mtimes[path] = current
                        cb = self._callbacks.get(path)
                        if cb is not None:
                            cb()
        except asyncio.CancelledError:
            pass

    def _schedule(self, coro: Awaitable[Any]) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.ensure_future(coro)

    async def _async_stop_watching(self) -> None:
        self._stop_watching()

    def _stop_watching(self) -> None:
        self._watched_paths = []
        self._callbacks.clear()
        self._mtimes.clear()
        self._branch_ref_path = None
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None

    async def get(self, key: str, compute: Callable[[], Awaitable[Any]]) -> Any:
        """Get a cached value by key; compute+cache on first access / after invalidation.

        Dirty is cleared BEFORE the async compute starts. A file change arriving during
        compute re-sets dirty, so the next ``get()`` re-reads rather than serving stale.
        """
        await self.ensure_started()
        existing = self._cache.get(key)
        if existing is not None and not existing["dirty"]:
            return existing["value"]
        if existing is not None:
            existing["dirty"] = False
        value = await compute()
        # Only update the cached value if no new invalidation arrived during compute.
        entry = self._cache.get(key)
        if entry is not None and not entry["dirty"]:
            entry["value"] = value
        if entry is None:
            self._cache[key] = {"value": value, "dirty": False, "compute": compute}
        return value

    def reset(self) -> None:
        """Reset all state. Stops file watchers. For testing only."""
        self._stop_watching()
        self._cache.clear()
        self._initialized = False
        self._init_task = None
        self._git_dir = None
        self._common_dir = None
        if self._unregister_cleanup is not None:
            self._unregister_cleanup()
            self._unregister_cleanup = None


_git_watcher = GitFileWatcher()


async def _compute_branch() -> str:
    git_dir = await resolve_git_dir()
    if not git_dir:
        return "HEAD"
    head = await read_git_head(git_dir)
    if not head:
        return "HEAD"
    return head["name"] if head["type"] == "branch" else "HEAD"


async def _compute_head() -> str:
    git_dir = await resolve_git_dir()
    if not git_dir:
        return ""
    head = await read_git_head(git_dir)
    if not head:
        return ""
    if head["type"] == "branch":
        return (await resolve_ref(git_dir, f"refs/heads/{head['name']}")) or ""
    return head["sha"]


async def _compute_remote_url() -> str | None:
    git_dir = await resolve_git_dir()
    if not git_dir:
        return None
    url = await parse_git_config_value(git_dir, "remote", "origin", "url")
    if url:
        return url
    # In worktrees, the config with remote URLs is in the common dir.
    common_dir = await get_common_dir(git_dir)
    if common_dir and common_dir != git_dir:
        return await parse_git_config_value(common_dir, "remote", "origin", "url")
    return None


async def _compute_default_branch() -> str:
    git_dir = await resolve_git_dir()
    if not git_dir:
        return "main"
    # refs/remotes/ lives in commonDir, not the per-worktree gitDir.
    common_dir = (await get_common_dir(git_dir)) or git_dir
    branch_from_symref = await read_symref_text(
        common_dir,
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/",
    )
    if branch_from_symref:
        return branch_from_symref
    for candidate in ("main", "master"):
        sha = await resolve_ref(common_dir, f"refs/remotes/origin/{candidate}")
        if sha:
            return candidate
    return "main"


async def get_cached_branch() -> str:
    return await _git_watcher.get("branch", _compute_branch)


async def get_cached_head() -> str:
    return await _git_watcher.get("head", _compute_head)


async def get_cached_remote_url() -> str | None:
    return await _git_watcher.get("remoteUrl", _compute_remote_url)


async def get_cached_default_branch() -> str:
    return await _git_watcher.get("defaultBranch", _compute_default_branch)


def reset_git_file_watcher() -> None:
    """Reset the git file watcher state. For testing only."""
    _git_watcher.reset()


async def get_head_for_dir(cwd: str) -> str | None:
    """Read the HEAD SHA for an arbitrary directory (not using the watcher)."""
    git_dir = await resolve_git_dir(cwd)
    if not git_dir:
        return None
    head = await read_git_head(git_dir)
    if not head:
        return None
    if head["type"] == "branch":
        return await resolve_ref(git_dir, f"refs/heads/{head['name']}")
    return head["sha"]


async def read_worktree_head_sha(worktree_path: str) -> str | None:
    """Read the HEAD SHA for a git worktree directory (not the main repo).

    Reads ``<worktreePath>/.git`` directly as a ``gitdir:`` pointer with no upward walk
    (unlike ``get_head_for_dir``, which would find the parent repo's ``.git`` if the worktree
    path doesn't exist). ``None`` if the worktree doesn't exist or is malformed.
    """
    try:
        with open(_osp.join(worktree_path, ".git"), encoding="utf-8") as fh:
            ptr = fh.read().strip()
    except OSError:
        return None
    if not ptr.startswith("gitdir:"):
        return None
    git_dir = _osp.abspath(_osp.join(worktree_path, ptr[len("gitdir:") :].strip()))

    head = await read_git_head(git_dir)
    if not head:
        return None
    if head["type"] == "branch":
        return await resolve_ref(git_dir, f"refs/heads/{head['name']}")
    return head["sha"]


async def get_remote_url_for_dir(cwd: str) -> str | None:
    """Read the remote origin URL for an arbitrary directory via ``.git/config``."""
    git_dir = await resolve_git_dir(cwd)
    if not git_dir:
        return None
    url = await parse_git_config_value(git_dir, "remote", "origin", "url")
    if url:
        return url
    common_dir = await get_common_dir(git_dir)
    if common_dir and common_dir != git_dir:
        return await parse_git_config_value(common_dir, "remote", "origin", "url")
    return None


async def is_shallow_clone() -> bool:
    """Whether we're in a shallow clone (``<commonDir>/shallow`` exists, per shallow.c)."""
    git_dir = await resolve_git_dir()
    if not git_dir:
        return False
    common_dir = (await get_common_dir(git_dir)) or git_dir
    return _osp.exists(_osp.join(common_dir, "shallow"))


async def get_worktree_count_from_fs() -> int:
    """Count worktrees by reading ``<commonDir>/worktrees/``. Main worktree adds 1."""
    try:
        git_dir = await resolve_git_dir()
        if not git_dir:
            return 0
        common_dir = (await get_common_dir(git_dir)) or git_dir
        entries = os.listdir(_osp.join(common_dir, "worktrees"))
        return len(entries) + 1
    except OSError:
        # No worktrees directory means only the main worktree.
        return 1


# ---------------------------------------------------------------------------
# Thin resolvers re-exported from the single git surface (oracle: src/utils/git.ts).
#
# import get_branch / get_default_branch / git_exe from one place.
# ---------------------------------------------------------------------------


def _git_exe() -> str:
    """Resolve the ``git`` executable. Falls back to bare ``git``."""
    return which_sync("git") or "git"


# memoizeWithLRU with a constant key (git_exe takes no args) — every spawn would otherwise re-do
# the PATH lookup; memoize so it happens once. ``git_exe()`` stays a callable.
git_exe = memoize_with_lru(_git_exe, lambda: "git")


async def get_head() -> str:
    return await get_cached_head()


async def get_branch() -> str:
    return await get_cached_branch()


async def get_default_branch() -> str:
    return await get_cached_default_branch()


async def get_remote_url() -> str | None:
    return await get_cached_remote_url()


async def dir_is_in_git_repo(cwd: str) -> bool:
    """Whether ``cwd`` is inside a git repo (oracle: ``src/utils/git.ts:dirIsInGitRepo``)."""
    return find_git_root(cwd) is not None
