"""Markdown config-file discovery

Walks managed / user / project ``.tabvis/<subdir>`` directories (commands, agents, skills, …) and
loads the ``*.md`` files with their parsed frontmatter, deduplicating files that resolve to the
same inode (symlinks / hard links). Used by the commands / agents / skills loaders.

Wire-key note: ``MarkdownFile`` dicts and the ``tengu_dir_search`` analytics payload keep their
camelCase keys (``filePath``/``baseDir``/``durationMs``/…) — they round-trip to consumers /
analytics, so they are NOT snake_cased.
"""

from __future__ import annotations

import asyncio
import os
import time
import unicodedata
from typing import Any, Literal

from tabvis.bootstrap.state import get_project_root
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy
from tabvis.utils.frontmatter_parser import FrontmatterData, parse_frontmatter
from tabvis.utils.git import find_canonical_git_root, find_git_root
from tabvis.utils.permissions.permission_setup import parse_tool_list_from_cli
from tabvis.utils.ripgrep import rip_grep
from tabvis.utils.settings.constants import SettingSource, get_enabled_setting_sources
from tabvis.utils.settings.managed_path import get_managed_file_path

# Tabvis configuration directory names
TABVIS_CONFIG_DIRECTORIES = (
    "commands",
    "agents",
    "output-styles",
    "skills",
    "workflows",
)

TabvisConfigDirectory = str  # one of TABVIS_CONFIG_DIRECTORIES

# MarkdownFile is a dict with keys: filePath, baseDir, frontmatter, content, source.
MarkdownFile = dict[str, Any]


def _normalize_path_for_comparison(file_path: str) -> str:
    """Normalize a path into a stable comparison key (Windows drive-letter casing).

    On POSIX this only normalizes backslashes to forward slashes, which is an effectively stable
    comparison key.
    """
    return file_path.replace("\\", "/")


def _is_setting_source_enabled(source: SettingSource) -> bool:
    """Whether a settings source is enabled.

    The clean-env default enables user/project/local, matching ``get_enabled_setting_sources``.
    """
    try:
        return source in get_enabled_setting_sources()
    except Exception:  # noqa: BLE001 - settings not available → enabled (clean-env default)
        return True


def _is_fs_inaccessible(error: object) -> bool:
    """Whether ``error`` is a "directory missing / inaccessible" filesystem error.

    ENOENT/ENOTDIR/EACCES (and their errno numbers) are treated as inaccessible (return ``[]`` /
    skip).
    """
    code = getattr(error, "errno", None)
    import errno as _errno

    if code in (_errno.ENOENT, _errno.ENOTDIR, _errno.EACCES):
        return True
    name = getattr(error, "code", None)
    return name in ("ENOENT", "ENOTDIR", "EACCES")


def extract_description_from_markdown(
    content: str,
    default_description: str = "Custom item",
) -> str:
    """Extract a description from markdown content.

    Uses the first non-empty line as the description, or falls back to a default.
    """
    import re

    lines = content.split("\n")
    for line in lines:
        trimmed = line.strip()
        if trimmed:
            # If it's a header, strip the header prefix
            header_match = re.match(r"^#+\s+(.+)$", trimmed)
            text = header_match.group(1) if header_match else trimmed

            # Return the text, limited to reasonable length
            return text[:97] + "..." if len(text) > 100 else text
    return default_description


def _parse_tool_list_string(tools_value: Any) -> list[str] | None:
    """Parse tools from frontmatter, supporting both string and array formats.

    Always returns a string array for consistency; ``None`` for missing/null (caller decides).
    """
    # Return None for missing/null - let caller decide the default
    if tools_value is None:
        return None

    # Empty string or other falsy values mean no tools
    if not tools_value:
        return []

    tools_array: list[str] = []
    if isinstance(tools_value, str):
        tools_array = [tools_value]
    elif isinstance(tools_value, list):
        tools_array = [item for item in tools_value if isinstance(item, str)]

    if len(tools_array) == 0:
        return []

    parsed_tools = parse_tool_list_from_cli(tools_array)
    if "*" in parsed_tools:
        return ["*"]
    return parsed_tools


def parse_agent_tools_from_frontmatter(tools_value: Any) -> list[str] | None:
    """Parse tools from agent frontmatter.

    Missing field = ``None`` (all tools); empty field = ``[]`` (no tools).
    """
    parsed = _parse_tool_list_string(tools_value)
    if parsed is None:
        # For agents: None = all tools (None), null = no tools ([])
        return None if tools_value is None else []
    # If parsed contains '*', return None (all tools)
    if "*" in parsed:
        return None
    return parsed


def parse_slash_command_tools_from_frontmatter(tools_value: Any) -> list[str]:
    """Parse allowed-tools from slash command frontmatter.

    Missing or empty field = no tools (``[]``).
    """
    parsed = _parse_tool_list_string(tools_value)
    if parsed is None:
        return []
    return parsed


def _get_file_identity(file_path: str) -> str | None:
    """Get a unique ``"device:inode"`` identifier for a file (or ``None`` if not stat'able).

    Allows detection of duplicate files accessed through different paths (e.g., symlinks).
    Returns ``None`` on error (fail open), so dedup may not work on some Windows configs.
    """
    try:
        stats = os.lstat(file_path)
        # Some filesystems (NFS, FUSE, network mounts) report dev=0 and ino=0 for all files,
        # which would make every file look like a duplicate. Skip dedup for these.
        if stats.st_dev == 0 and stats.st_ino == 0:
            return None
        return f"{stats.st_dev}:{stats.st_ino}"
    except OSError:
        return None


def _resolve_stop_boundary(cwd: str) -> str | None:
    """Compute the stop boundary for :func:`get_project_dirs_up_to_home`'s upward walk.

    Normally the walk stops at the nearest ``.git`` above ``cwd``. But if the Bash tool has
    ``cd``'d into a nested git repo inside the session's project, the boundary is widened to the
    session's git root.
    """
    cwd_git_root = find_git_root(cwd)
    session_git_root = find_git_root(get_project_root())
    if not cwd_git_root or not session_git_root:
        return cwd_git_root
    # findCanonicalGitRoot resolves worktree `.git` files to the main repo.
    # Submodules (no commondir) and standalone clones fall through unchanged.
    cwd_canonical = find_canonical_git_root(cwd)
    if cwd_canonical and _normalize_path_for_comparison(
        cwd_canonical
    ) == _normalize_path_for_comparison(session_git_root):
        # Same canonical repo (main, or a worktree of main). Stop at nearest .git.
        return cwd_git_root
    # Different canonical repo. Is it nested *inside* the session's project?
    n_cwd_git_root = _normalize_path_for_comparison(cwd_git_root)
    n_session_root = _normalize_path_for_comparison(session_git_root)
    if n_cwd_git_root != n_session_root and n_cwd_git_root.startswith(n_session_root + os.sep):
        # Nested repo inside the project — skip past it, stop at the project's root.
        return session_git_root
    # Sibling repo or elsewhere. Stop at nearest .git (old behavior).
    return cwd_git_root


def get_project_dirs_up_to_home(subdir: TabvisConfigDirectory, cwd: str) -> list[str]:
    """Traverse from ``cwd`` up to the git root (or home), collecting ``.tabvis/<subdir>`` dirs.

    Stopping at git root prevents commands/skills from parent directories outside the repository
    from leaking into projects.

    :returns: Directory paths containing ``.tabvis/<subdir>``, most-specific (cwd) first.
    """
    home = unicodedata.normalize("NFC", os.path.realpath(os.path.expanduser("~")))
    git_root = _resolve_stop_boundary(cwd)
    current = os.path.realpath(cwd)
    dirs: list[str] = []

    # Traverse from current directory up to git root (or home if not in a git repo)
    while True:
        # Stop if we've reached the home directory (loaded separately as userDir).
        if _normalize_path_for_comparison(current) == _normalize_path_for_comparison(home):
            break

        tabvis_subdir = os.path.join(current, ".tabvis", subdir)
        # Filter to existing dirs (perf filter + worktree fallback relies on it).
        # statSync + explicit error handling: re-throw unexpected errors.
        try:
            os.stat(tabvis_subdir)
            dirs.append(tabvis_subdir)
        except OSError as e:
            if not _is_fs_inaccessible(e):
                raise

        # Stop after processing the git root directory.
        if git_root and _normalize_path_for_comparison(
            current
        ) == _normalize_path_for_comparison(git_root):
            break

        # Move to parent directory
        parent = os.path.dirname(current)

        # Safety check: if parent is the same as current, we've reached the root
        if parent == current:
            break

        current = parent

    return dirs


# ----------------------------------------------------------------------------------------------
# Memoized loader (lodash memoize → keyed async cache)
# ----------------------------------------------------------------------------------------------

# lodash `memoize` caches the *resolved* Promise keyed by `${subdir}:${cwd}`. We mirror that with
# a per-key cache that stores the awaited result.
_load_cache: dict[str, list[MarkdownFile]] = {}


async def load_markdown_files_for_subdir(
    subdir: TabvisConfigDirectory,
    cwd: str,
) -> list[MarkdownFile]:
    """Load markdown files from managed, user, and project directories.

    Memoized on ``f"{subdir}:{cwd}"`` (lodash ``memoize`` with a custom resolver).
    """
    cache_key = f"{subdir}:{cwd}"
    if cache_key in _load_cache:
        return _load_cache[cache_key]
    result = await _load_markdown_files_for_subdir(subdir, cwd)
    _load_cache[cache_key] = result
    return result


def _reset_load_cache_for_tests() -> None:
    """Clear the memoization cache (tests only)."""
    _load_cache.clear()


# Expose a `.cache` handle resembling lodash's `memoize.cache` for parity.
load_markdown_files_for_subdir.cache = _load_cache  # type: ignore[attr-defined]


async def _load_markdown_files_for_subdir(
    subdir: TabvisConfigDirectory,
    cwd: str,
) -> list[MarkdownFile]:
    search_start_time = time.time() * 1000.0
    user_dir = os.path.join(get_tabvis_config_home_dir(), subdir)
    managed_dir = os.path.join(get_managed_file_path(), ".tabvis", subdir)
    project_dirs = get_project_dirs_up_to_home(subdir, cwd)

    # For git worktrees where the worktree does NOT have .tabvis/<subdir> checked out, fall back to
    # the main repository's copy. getProjectDirsUpToHome stops at the worktree root, so it never
    # sees the main repo on its own. Only add the main repo's copy when the worktree root's
    # .tabvis/<subdir> is absent (a standard worktree add already has identical content).
    git_root = find_git_root(cwd)
    canonical_root = find_canonical_git_root(cwd)
    if git_root and canonical_root and canonical_root != git_root:
        worktree_subdir = _normalize_path_for_comparison(
            os.path.join(git_root, ".tabvis", subdir)
        )
        worktree_has_subdir = any(
            _normalize_path_for_comparison(d) == worktree_subdir for d in project_dirs
        )
        if not worktree_has_subdir:
            main_tabvis_subdir = os.path.join(canonical_root, ".tabvis", subdir)
            if main_tabvis_subdir not in project_dirs:
                project_dirs.append(main_tabvis_subdir)

    async def _load_managed() -> list[MarkdownFile]:
        loaded = await _load_markdown_files(managed_dir)
        return [
            {**file, "baseDir": managed_dir, "source": "policySettings"} for file in loaded
        ]

    async def _load_user() -> list[MarkdownFile]:
        if not _is_setting_source_enabled("userSettings"):
            return []
        loaded = await _load_markdown_files(user_dir)
        return [{**file, "baseDir": user_dir, "source": "userSettings"} for file in loaded]

    async def _load_project() -> list[list[MarkdownFile]]:
        if not _is_setting_source_enabled("projectSettings"):
            return []

        async def _load_one(project_dir: str) -> list[MarkdownFile]:
            loaded = await _load_markdown_files(project_dir)
            return [
                {**file, "baseDir": project_dir, "source": "projectSettings"} for file in loaded
            ]

        return await asyncio.gather(*(_load_one(d) for d in project_dirs))

    managed_files, user_files, project_files_nested = await asyncio.gather(
        _load_managed(),
        _load_user(),
        _load_project(),
    )

    # Flatten nested project files array
    project_files = [file for group in project_files_nested for file in group]

    # Combine all files with priority: managed > user > project
    all_files = [*managed_files, *user_files, *project_files]

    # Deduplicate files that resolve to the same physical file (same inode).
    file_identities = await asyncio.gather(
        *(asyncio.to_thread(_get_file_identity, file["filePath"]) for file in all_files)
    )

    seen_file_ids: dict[str, SettingSource] = {}
    deduplicated_files: list[MarkdownFile] = []

    for i, file in enumerate(all_files):
        file_id = file_identities[i]
        if file_id is None:
            # If we can't identify the file, include it (fail open)
            deduplicated_files.append(file)
            continue
        existing_source = seen_file_ids.get(file_id)
        if existing_source is not None:
            log_for_debugging(
                f"Skipping duplicate file '{file['filePath']}' from {file['source']} "
                f"(same inode already loaded from {existing_source})",
            )
            continue
        seen_file_ids[file_id] = file["source"]
        deduplicated_files.append(file)

    duplicates_removed = len(all_files) - len(deduplicated_files)
    if duplicates_removed > 0:
        log_for_debugging(
            f"Deduplicated {duplicates_removed} files in {subdir} "
            f"(same inode via symlinks or hard links)",
        )

    return deduplicated_files


async def _find_markdown_files_native(dir_path: str, deadline: float) -> list[str]:
    """Native implementation to find markdown files using stdlib (ripgrep fallback).

    Exists alongside ripgrep because ripgrep has poor startup performance in native builds and
    provides a fallback when ripgrep is unavailable (or ``TABVIS_USE_NATIVE_FILE_SEARCH`` is set).

    Symlink handling: follows symlinks; uses device+inode tracking to detect cycles; falls back
    to realpath on systems without inode support. Does not respect ``.gitignore``.

    :param deadline: ``time.monotonic()`` deadline; the walk aborts once it is exceeded
        (AbortSignal.timeout analogue).
    """
    files: list[str] = []
    visited_dirs: set[str] = set()

    def _aborted() -> bool:
        return time.monotonic() > deadline

    async def walk(current_dir: str) -> None:
        if _aborted():
            return

        # Cycle detection: track visited directories by device+inode.
        try:
            stats = await asyncio.to_thread(os.stat, current_dir)
            if os.path.isdir(current_dir):
                dir_key = (
                    f"{stats.st_dev}:{stats.st_ino}"
                    if stats.st_dev is not None and stats.st_ino is not None
                    else await asyncio.to_thread(os.path.realpath, current_dir)
                )

                if dir_key in visited_dirs:
                    log_for_debugging(
                        f"Skipping already visited directory (circular symlink): {current_dir}",
                    )
                    return
                visited_dirs.add(dir_key)
        except OSError as error:
            log_for_debugging(f"Failed to stat directory {current_dir}: {error}")
            return

        try:
            entries = await asyncio.to_thread(lambda: list(os.scandir(current_dir)))

            for entry in entries:
                if _aborted():
                    break

                full_path = os.path.join(current_dir, entry.name)

                try:
                    # Handle symlinks: is_file()/is_dir() w/ follow=False return False for links.
                    if entry.is_symlink():
                        try:
                            # stat() follows symlinks
                            if os.path.isdir(full_path):
                                await walk(full_path)
                            elif os.path.isfile(full_path) and entry.name.endswith(".md"):
                                files.append(full_path)
                        except OSError as error:
                            log_for_debugging(f"Failed to follow symlink {full_path}: {error}")
                    elif entry.is_dir():
                        await walk(full_path)
                    elif entry.is_file() and entry.name.endswith(".md"):
                        files.append(full_path)
                except OSError as error:
                    # Skip files/directories we can't access
                    log_for_debugging(f"Failed to access {full_path}: {error}")
        except OSError as error:
            # If readdir fails (e.g., permission denied), log and continue
            log_for_debugging(f"Failed to read directory {current_dir}: {error}")

    await walk(dir_path)
    return files


async def _load_markdown_files(dir_path: str) -> list[MarkdownFile]:
    """Load markdown files from ``dir`` (e.g., ``~/.tabvis/commands``) with parsed frontmatter."""
    # File search strategy:
    # - Default: ripgrep (faster, battle-tested)
    # - Fallback: native stdlib (when TABVIS_USE_NATIVE_FILE_SEARCH is set)
    use_native = is_env_truthy(os.environ.get("TABVIS_USE_NATIVE_FILE_SEARCH"))
    deadline = time.monotonic() + 3.0  # AbortSignal.timeout(3000)
    try:
        if use_native:
            files = await _find_markdown_files_native(dir_path, deadline)
        else:
            # NOTE: the existing ``rip_grep`` takes ``(args, target)`` and manages its own timeout;
            # the TS ``signal`` arg has no analogue here.
            files = await rip_grep(
                ["--files", "--hidden", "--follow", "--no-ignore", "--glob", "*.md"],
                dir_path,
            )
    except OSError as e:
        # Handle missing/inaccessible dir directly (TOCTOU).
        if _is_fs_inaccessible(e):
            return []
        raise

    async def _read_one(file_path: str) -> MarkdownFile | None:
        try:
            raw_content = await asyncio.to_thread(
                lambda: open(file_path, encoding="utf-8").read()  # noqa: SIM115
            )
            parsed = parse_frontmatter(raw_content, file_path)
            frontmatter: FrontmatterData = parsed["frontmatter"]
            content: str = parsed["content"]

            return {
                "filePath": file_path,
                "frontmatter": frontmatter,
                "content": content,
            }
        except OSError as error:
            log_for_debugging(f"Failed to read/parse markdown file:  {file_path}: {error}")
            return None

    results = await asyncio.gather(*(_read_one(file_path) for file_path in files))

    return [r for r in results if r is not None]


__all__ = [
    "TABVIS_CONFIG_DIRECTORIES",
    "TabvisConfigDirectory",
    "MarkdownFile",
    "extract_description_from_markdown",
    "get_project_dirs_up_to_home",
    "load_markdown_files_for_subdir",
    "parse_agent_tools_from_frontmatter",
    "parse_slash_command_tools_from_frontmatter",
]

_ = Literal  # forward-parity placeholder (Literal used in type narration above)
