"""Directory-access + read/write permission resolution.

This is the big filesystem permission module: the directory-access rules (working directories +
additional-working-directory handling), the read/write filesystem permission resolution, the
internal-harness path carve-outs (plans / scratchpad / project-dir / tool-results / memory), the
gitignore-style pattern matching against deny/ask/allow rules, and the auto-edit safety checks
(dangerous files/dirs, suspicious Windows path patterns, Tabvis config files).

Casing: Python identifiers are snake_case. Dict-shaped data that round-trips to the
transcript / settings / SDK keeps its camelCase wire keys verbatim — the permission-decision
shapes (``behavior`` / ``message`` / ``updatedInput`` / ``decisionReason`` / ``suggestions``), the
:data:`PermissionUpdate` payloads (``type`` / ``rules`` / ``toolName`` / ``ruleContent`` /
``behavior`` / ``destination`` / ``directories`` / ``mode``), and the context collections
(``additionalWorkingDirectories`` / ``alwaysAllowRules`` / …).

Import-cycle break (``filesystem`` ↔ ``permission_update``): ``permission_update`` lazy-imports
:func:`to_posix_path` from here; this module needs :func:`create_read_rule_suggestion` from
``permission_update`` only inside :func:`generate_suggestions`, so that import is **function-local**
and the type-only ref lives under ``if TYPE_CHECKING``. ``filesystem`` therefore imports STANDALONE.

Library substitutions (per the implementation plan):
- npm ``ignore`` (gitignore matcher) → ``pathspec`` (``GitWildMatchPattern``), already a dep.
- npm ``lodash-es/memoize`` → :func:`tabvis.utils.memoize.memoize_with_lru` (identity-keyed,
  no-eviction for the nullary cases).
- Node ``os``/``path`` builtins → :mod:`os` / :mod:`posixpath` / :mod:`os.path`.

Inlined (no flat-tool home) — faithful values:
- ``TABVIS_FOLDER_PERMISSION_PATTERN`` / ``GLOBAL_TABVIS_FOLDER_PERMISSION_PATTERN`` (from
  ``src/tools/FileEditTool/constants.ts``).
- ``is_agent_memory_path`` (from ``src/tools/AgentTool/agentMemory.ts``).
- ``is_auto_mem_path`` (the path containment check remains local to this permission module).
- ``get_settings_root_path_for_source`` (from ``src/utils/settings/settings.ts`` — not implemented here).
- ``get_rule_by_contents_for_tool_name`` (from ``src/utils/permissions/permissions.ts`` — the existing
  ``permissions.py`` has the source-ordered rule extractors but not this contents-map helper).
"""

from __future__ import annotations

import os
import os.path
import posixpath
import unicodedata
from typing import TYPE_CHECKING, Any

from pathspec.patterns.gitwildmatch import GitIgnoreSpecPattern

from tabvis.bootstrap.state import get_original_cwd, get_session_id
from tabvis.agent.mem.paths import (
    get_auto_mem_path,
    get_memory_base_dir,
    has_auto_mem_path_override,
)
from tabvis.agent.tools.file_edit_tool import FILE_EDIT_TOOL_NAME
from tabvis.agent.tools.file_read_tool import FILE_READ_TOOL_NAME
from tabvis.utils.cwd import get_cwd
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.fs_operations import (
    get_fs_implementation,
    get_paths_for_permission_check,
)
from tabvis.utils.memoize import memoize_with_lru
from tabvis.utils.path import (
    contains_path_traversal,
    expand_path,
    get_directory_for_path,
    sanitize_path,
)
from tabvis.utils.permissions.permissions import (
    get_allow_rules,
    get_ask_rules,
    get_deny_rules,
)
from tabvis.utils.platform import get_platform
from tabvis.utils.session_storage import get_project_dir
from tabvis.utils.settings.constants import (
    SETTING_SOURCES,
    get_settings_file_path_for_source,
)
from tabvis.utils.shell.read_only_command_validation import (
    contains_vulnerable_unc_path,
)
from tabvis.utils.tool_result_storage import get_tool_results_dir
from tabvis.utils.windows_paths import windows_path_to_posix_path

if TYPE_CHECKING:
    from tabvis.tool import Tool
    from tabvis.types.permissions import (
        PermissionDecision,
        PermissionResult,
        PermissionRule,
        PermissionRuleSource,
        PermissionUpdate,
        ToolPermissionContext,
    )

# --- Inlined flat-tool / cross-module constants (faithful values) ------------------------

# tabvis.agent.tools.file_edit_tool. Values byte-faithful to the TS source.
TABVIS_FOLDER_PERMISSION_PATTERN = "/.tabvis/**"
GLOBAL_TABVIS_FOLDER_PERMISSION_PATTERN = "~/.tabvis/**"


# --- Dangerous files / directories -------------------------------------------------------

#: Dangerous files that should be protected from auto-editing. These files can be used for code
#: execution or data exfiltration.
DANGEROUS_FILES: tuple[str, ...] = (
    ".gitconfig",
    ".gitmodules",
    ".bashrc",
    ".bash_profile",
    ".zshrc",
    ".zprofile",
    ".profile",
    ".ripgreprc",
    ".mcp.json",
    ".tabvis.json",
)

#: Dangerous directories that should be protected from auto-editing. These directories contain
#: sensitive configuration or executable files.
DANGEROUS_DIRECTORIES: tuple[str, ...] = (
    ".git",
    ".vscode",
    ".idea",
    ".tabvis",
)


def normalize_case_for_comparison(path: str) -> str:
    """Normalize a path for case-insensitive comparison.

    Prevents bypassing security checks using mixed-case paths on case-insensitive filesystems
    (macOS/Windows) like ``.cLauDe/Settings.locaL.json``. Always lowercases regardless of platform
    for consistent security.
    """
    return path.lower()


def get_tabvis_skill_scope(file_path: str) -> dict[str, str] | None:
    """If ``file_path`` is inside a ``.tabvis/skills/{name}/`` directory (project or global), return
    ``{"skillName", "pattern"}`` scoped to just that skill, else ``None``.

    Used to offer a narrower "allow edits to this skill only" option in the permission dialog /
    SDK suggestions, so iterating on one skill doesn't require granting session access to all of
    ``.tabvis/`` (settings.json, hooks/, etc.).
    """
    absolute_path = expand_path(file_path)
    absolute_path_lower = normalize_case_for_comparison(absolute_path)

    bases = [
        {
            "dir": expand_path(os.path.join(get_original_cwd(), ".tabvis", "skills")),
            "prefix": "/.tabvis/skills/",
        },
        {
            "dir": expand_path(os.path.join(os.path.expanduser("~"), ".tabvis", "skills")),
            "prefix": "~/.tabvis/skills/",
        },
    ]

    for base in bases:
        dir_ = base["dir"]
        prefix = base["prefix"]
        dir_lower = normalize_case_for_comparison(dir_)
        # Try both path separators (Windows paths may not be normalized to /).
        for s in (os.sep, "/"):
            if absolute_path_lower.startswith(dir_lower + s.lower()):
                # Match on lowercase, but slice the ORIGINAL path so the skill name preserves case
                # (pattern matching downstream is case-sensitive).
                rest = absolute_path[len(dir_) + len(s) :]
                slash = rest.find("/")
                bslash = rest.find("\\") if os.sep == "\\" else -1
                if slash == -1:
                    cut = bslash
                elif bslash == -1:
                    cut = slash
                else:
                    cut = min(slash, bslash)
                # Require a separator: file must be INSIDE the skill dir, not a file directly under
                # skills/ (no skill scope for that).
                if cut <= 0:
                    return None
                skill_name = rest[:cut]
                # Reject traversal and empty. Use ``'..' in`` (not ``== '..'``) to match step 1.6's
                # ruleContent.includes('..') guard.
                if not skill_name or skill_name == "." or ".." in skill_name:
                    return None
                # Reject glob metacharacters: skillName is interpolated into a gitignore pattern.
                if any(c in skill_name for c in "*?[]"):
                    return None
                return {"skillName": skill_name, "pattern": prefix + skill_name + "/**"}

    return None


# Always use / as the path separator per gitignore spec
# https://git-scm.com/docs/gitignore
DIR_SEP = posixpath.sep


def relative_path(from_: str, to: str) -> str:
    """Cross-platform relative path calculation returning POSIX-style paths.

    Handles Windows path conversion internally.
    """
    if get_platform() == "windows":
        posix_from = windows_path_to_posix_path(from_)
        posix_to = windows_path_to_posix_path(to)
        return posixpath.relpath(posix_to, posix_from)
    return posixpath.relpath(to, from_)


def to_posix_path(path: str) -> str:
    """Convert a path to POSIX format for pattern matching (Windows-aware)."""
    if get_platform() == "windows":
        return windows_path_to_posix_path(path)
    return path


def _get_settings_paths() -> list[str]:
    paths = [get_settings_file_path_for_source(source) for source in SETTING_SOURCES]
    return [p for p in paths if p is not None]


def is_tabvis_settings_path(file_path: str) -> bool:
    """Whether ``file_path`` is a Tabvis settings file (``.tabvis/settings.json`` /
    ``.tabvis/settings.local.json`` for any project, or the current project's settings files)."""
    # SECURITY: Normalize path structure first to prevent bypass via redundant ./ sequences like
    # ``./.tabvis/./settings.json`` which would evade the endswith() check.
    expanded_path = expand_path(file_path)

    # Normalize for case-insensitive comparison.
    normalized_path = normalize_case_for_comparison(expanded_path)

    # Use platform separator so endswith checks work on both Unix (/) and Windows (\).
    if normalized_path.endswith(f"{os.sep}.tabvis{os.sep}settings.json") or normalized_path.endswith(
        f"{os.sep}.tabvis{os.sep}settings.local.json"
    ):
        # Include .tabvis/settings.json even for other projects.
        return True
    # Current project's settings files (managed settings + CLI args). Both absolute + normalized.
    return any(
        normalize_case_for_comparison(settings_path) == normalized_path
        for settings_path in _get_settings_paths()
    )


def _is_tabvis_config_file_path(file_path: str) -> bool:
    """Always ask when Tabvis tries to edit its own config files."""
    if is_tabvis_settings_path(file_path):
        return True

    # Check if file is within .tabvis/commands, .tabvis/agents, or .tabvis/skills using proper path
    # segment validation (path_in_working_path handles case-insensitive comparison).
    commands_dir = os.path.join(get_original_cwd(), ".tabvis", "commands")
    agents_dir = os.path.join(get_original_cwd(), ".tabvis", "agents")
    skills_dir = os.path.join(get_original_cwd(), ".tabvis", "skills")

    return (
        path_in_working_path(file_path, commands_dir)
        or path_in_working_path(file_path, agents_dir)
        or path_in_working_path(file_path, skills_dir)
    )


def _is_session_plan_file(absolute_path: str) -> bool:
    """Whether ``absolute_path`` is the plan file for the current session (main or agent-specific)."""
    # Lazy import: plans was implemented earlier this workflow; keep filesystem importable even if the
    # import ordering shifts.
    from tabvis.utils.plans import get_plan_slug, get_plans_directory

    expected_prefix = os.path.join(get_plans_directory(), get_plan_slug())
    # SECURITY: Normalize to prevent path traversal bypasses via .. segments.
    normalized_path = os.path.normpath(absolute_path)
    return normalized_path.startswith(expected_prefix) and normalized_path.endswith(".md")


def _is_project_dir_path(absolute_path: str) -> bool:
    """Whether ``absolute_path`` is within the current project's directory
    (``~/.tabvis/projects/{sanitized-cwd}/...``)."""
    project_dir = get_project_dir(get_cwd())
    # SECURITY: Normalize to prevent path traversal bypasses via .. segments.
    normalized_path = os.path.normpath(absolute_path)
    return normalized_path == project_dir or normalized_path.startswith(project_dir + os.sep)


def is_scratchpad_enabled() -> bool:
    """Whether the scratchpad directory feature is enabled (the ``tengu_scratch`` Statsig gate)."""
    return False


def get_tabvis_temp_dir_name() -> str:
    """User-specific Tabvis temp directory name.

    On Unix: ``tabvis-{uid}`` (prevents multi-user permission conflicts). On Windows: ``tabvis``
    (tmpdir() is already per-user).
    """
    if get_platform() == "windows":
        return "tabvis"
    # Use UID to create per-user directories.
    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid is not None else 0
    return f"tabvis-{uid}"


def _compute_tabvis_temp_dir() -> str:
    import tempfile

    base_tmp_dir = os.environ.get("TABVIS_TMPDIR") or (
        tempfile.gettempdir() if get_platform() == "windows" else "/tmp"
    )

    # Resolve symlinks in the base temp directory (e.g. /tmp -> /private/tmp on macOS).
    fs = get_fs_implementation()
    resolved_base_tmp_dir = base_tmp_dir
    try:
        resolved_base_tmp_dir = fs.realpath_sync(base_tmp_dir)
    except OSError:
        # If resolution fails, use the original path.
        pass

    return os.path.join(resolved_base_tmp_dir, get_tabvis_temp_dir_name()) + os.sep


# Memoized: inputs (TABVIS_TMPDIR env + platform) are fixed at startup and the realpath of the system
# tmp dir does not change mid-session. lodash memoize() with no resolver keys on the first arg; this
# function is nullary so a single cached value is correct (parity).
_TABVIS_TEMP_DIR_MEMO = memoize_with_lru(lambda _key: _compute_tabvis_temp_dir(), lambda _key: _key, 1)


def get_tabvis_temp_dir() -> str:
    """Tabvis temp directory path (with trailing sep) and symlinks resolved.

    Uses ``TABVIS_TMPDIR`` if set, else ``/tmp/tabvis-{uid}/`` (Unix, resolved to ``/private/tmp/...``
    on macOS) or ``{tmpdir}/tabvis/`` (Windows).
    """
    return _TABVIS_TEMP_DIR_MEMO("")


def get_project_temp_dir() -> str:
    """Project temp directory path with trailing separator (``/tmp/tabvis-{uid}/{sanitized-cwd}/``)."""
    return os.path.join(get_tabvis_temp_dir(), sanitize_path(get_original_cwd())) + os.sep


def get_scratchpad_dir() -> str:
    """Scratchpad directory path for the current session
    (``/tmp/tabvis-{uid}/{sanitized-cwd}/{sessionId}/scratchpad/``)."""
    return os.path.join(get_project_temp_dir(), str(get_session_id()), "scratchpad")


async def ensure_scratchpad_dir() -> str:
    """Ensure the scratchpad directory exists for the current session (mode 0o700). Returns the path.

    Raises:
        RuntimeError: if the scratchpad feature is not enabled.
    """
    if not is_scratchpad_enabled():
        raise RuntimeError("Scratchpad directory feature is not enabled")

    fs = get_fs_implementation()
    scratchpad_dir = get_scratchpad_dir()

    # Create directory recursively with secure permissions (owner-only access). mkdir handles
    # recursive: true internally and is a no-op if the dir exists.
    await fs.mkdir(scratchpad_dir, {"mode": 0o700})

    return scratchpad_dir


def _is_scratchpad_path(absolute_path: str) -> bool:
    """Whether ``absolute_path`` is within the scratchpad directory."""
    if not is_scratchpad_enabled():
        return False
    scratchpad_dir = get_scratchpad_dir()
    # SECURITY: Normalize the path to resolve .. segments before checking (prevents traversal
    # bypasses like ``.../scratchpad/../../../etc/passwd``).
    normalized_path = os.path.normpath(absolute_path)
    return normalized_path == scratchpad_dir or normalized_path.startswith(scratchpad_dir + os.sep)


# --- Memdir / agent-memory carve-out helpers ---------------------------------------------


def _is_auto_mem_path(absolute_path: str) -> bool:
    """Whether ``absolute_path`` is within the auto-memory directory.

    Kept local because this check is specific to filesystem permission resolution.
    """
    # SECURITY: Normalize to prevent path traversal bypasses via .. segments.
    normalized_path = os.path.normpath(absolute_path)
    return normalized_path.startswith(get_auto_mem_path())


def _is_agent_memory_path(absolute_path: str) -> bool:
    """Whether ``absolute_path`` is within an agent-memory directory (self-improving agents).

    Inlined because ``tabvis.agent.tools.agent_tool`` does not expose it. Faithful to the
    user/project/local scope checks.
    """
    # SECURITY: Normalize to prevent path traversal bypasses via .. segments.
    normalized_path = os.path.normpath(absolute_path)
    memory_base = get_memory_base_dir()

    # User scope: check memory base (may be a custom dir or the config home).
    if normalized_path.startswith(os.path.join(memory_base, "agent-memory") + os.sep):
        return True

    # Project scope: always cwd-based (not redirected).
    if normalized_path.startswith(os.path.join(get_cwd(), ".tabvis", "agent-memory") + os.sep):
        return True

    # Local scope: persisted to mount when TABVIS_REMOTE_MEMORY_DIR is set, otherwise cwd-based.
    remote_memory_dir = os.environ.get("TABVIS_REMOTE_MEMORY_DIR")
    if remote_memory_dir:
        if (os.sep + "agent-memory-local" + os.sep) in normalized_path and normalized_path.startswith(
            os.path.join(remote_memory_dir, "projects") + os.sep
        ):
            return True
    elif normalized_path.startswith(os.path.join(get_cwd(), ".tabvis", "agent-memory-local") + os.sep):
        return True

    return False


def _is_dangerous_file_path_to_auto_edit(path: str) -> bool:
    """Whether ``path`` is dangerous to auto-edit without explicit permission.

    Includes files in .git / .vscode / .idea / .tabvis dirs (with the ``.tabvis/worktrees/`` structural
    carve-out), shell config files, and UNC paths.
    """
    absolute_path = expand_path(path)
    path_segments = absolute_path.split(os.sep)
    file_name = path_segments[-1] if path_segments else None

    # Check for UNC paths (defense-in-depth): block anything starting with \\ or //.
    if path.startswith("\\\\") or path.startswith("//"):
        return True

    # Check if path is within dangerous directories (case-insensitive).
    for i in range(len(path_segments)):
        segment = path_segments[i]
        normalized_segment = normalize_case_for_comparison(segment)

        for dir_ in DANGEROUS_DIRECTORIES:
            if normalized_segment != normalize_case_for_comparison(dir_):
                continue

            # Special case: .tabvis/worktrees/ is a structural path (where Tabvis stores git
            # worktrees), not a user-created dangerous directory. Skip the .tabvis segment when it's
            # followed by 'worktrees'. Any nested .tabvis dirs within the worktree are still blocked.
            if dir_ == ".tabvis":
                next_segment = path_segments[i + 1] if i + 1 < len(path_segments) else None
                if next_segment and normalize_case_for_comparison(next_segment) == "worktrees":
                    break  # Skip this .tabvis, continue checking other segments.

            return True

    # Check for dangerous configuration files (case-insensitive).
    if file_name:
        normalized_file_name = normalize_case_for_comparison(file_name)
        if any(
            normalize_case_for_comparison(dangerous_file) == normalized_file_name
            for dangerous_file in DANGEROUS_FILES
        ):
            return True

    return False


_DOS_DEVICE_NAMES = ("CON", "PRN", "AUX", "NUL")


def _has_suspicious_windows_path_pattern(path: str) -> bool:
    """Detect suspicious Windows path patterns that could bypass security checks.

    NTFS Alternate Data Streams, 8.3 short names, long path prefixes, trailing dots/spaces, DOS
    device names, three-or-more consecutive dots as a path component, and vulnerable UNC paths.
    Checked on all platforms (NTFS can be mounted on Linux/macOS); the ADS colon check is
    Windows/WSL-only.
    """
    import re

    # NTFS Alternate Data Streams. Look for ':' after position 2 to skip drive letters (C:\).
    if get_platform() in ("windows", "wsl"):
        colon_index = path.find(":", 2)
        if colon_index != -1:
            return True

    # 8.3 short names: '~' followed by a digit (GIT~1, CLAUDE~1, SETTIN~1.JSON, …).
    if re.search(r"~\d", path):
        return True

    # Long path prefixes (both backslash and forward slash variants).
    if (
        path.startswith("\\\\?\\")
        or path.startswith("\\\\.\\")
        or path.startswith("//?/")
        or path.startswith("//./")
    ):
        return True

    # Trailing dots and spaces that Windows strips during path resolution.
    if re.search(r"[.\s]+$", path):
        return True

    # DOS device names Windows treats as special devices (CON, PRN, AUX, NUL, COM1-9, LPT1-9).
    if re.search(r"\.(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", path, re.IGNORECASE):
        return True

    # Three or more consecutive dots used as a path component (bounded by separators or boundaries).
    if re.search(r"(^|/|\\)\.{3,}(/|\\|$)", path):
        return True

    # UNC paths (all platforms, defense-in-depth).
    if contains_vulnerable_unc_path(path):
        return True

    return False


def check_path_safety_for_auto_edit(
    path: str,
    precomputed_paths_to_check: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Whether ``path`` is safe for auto-editing (acceptEdits mode).

    Returns ``{"safe": True}`` if all checks pass, else ``{"safe": False, "message": str}``. Checks
    BOTH the original path AND resolved symlink paths to prevent symlink bypasses.
    """
    paths_to_check = (
        precomputed_paths_to_check
        if precomputed_paths_to_check is not None
        else get_paths_for_permission_check(path)
    )

    # Suspicious Windows path patterns.
    for path_to_check in paths_to_check:
        if _has_suspicious_windows_path_pattern(path_to_check):
            return {
                "safe": False,
                "message": (
                    f"Tabvis requested permissions to write to {path}, which contains a suspicious "
                    "Windows path pattern that requires manual approval."
                ),
            }

    # Tabvis config files.
    for path_to_check in paths_to_check:
        if _is_tabvis_config_file_path(path_to_check):
            return {
                "safe": False,
                "message": (
                    f"Tabvis requested permissions to write to {path}, but you haven't granted it yet."
                ),
            }

    # Dangerous files.
    for path_to_check in paths_to_check:
        if _is_dangerous_file_path_to_auto_edit(path_to_check):
            return {
                "safe": False,
                "message": f"Tabvis requested permissions to edit {path} which is a sensitive file.",
            }

    return {"safe": True}


def all_working_directories(context: ToolPermissionContext) -> set[str]:
    """The set of working directories: the original cwd + every additional working directory."""
    return {
        get_original_cwd(),
        *context.get("additionalWorkingDirectories", {}).keys(),
    }


# Working directories are session-stable; memoize their resolved forms to avoid repeated
# existsSync/lstatSync/realpathSync syscalls on every permission check. Keyed by path string.
_get_resolved_working_dir_paths = memoize_with_lru(
    get_paths_for_permission_check, lambda p: p, 500
)


def get_resolved_working_dir_paths(working_path: str) -> list[str]:
    """Resolved symlink forms of a working directory (memoized)."""
    return _get_resolved_working_dir_paths(working_path)


def path_in_allowed_working_path(
    path: str,
    tool_permission_context: ToolPermissionContext,
    precomputed_paths_to_check: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Whether ``path`` (and every resolved symlink form) is within an allowed working directory."""
    paths_to_check = (
        precomputed_paths_to_check
        if precomputed_paths_to_check is not None
        else get_paths_for_permission_check(path)
    )

    # Resolve working directories the same way we resolve input paths so comparisons are symmetric.
    working_paths: list[str] = []
    for wp in all_working_directories(tool_permission_context):
        working_paths.extend(get_resolved_working_dir_paths(wp))

    # All paths must be within allowed working paths; if any resolved path is outside, deny access.
    return all(
        any(path_in_working_path(path_to_check, working_path) for working_path in working_paths)
        for path_to_check in paths_to_check
    )


def path_in_working_path(path: str, working_path: str) -> bool:
    """Whether ``path`` is inside ``working_path`` (macOS symlink + case normalization aware)."""
    import re

    absolute_path = expand_path(path)
    absolute_working_path = expand_path(working_path)

    # On macOS, handle common symlink issues (/var -> /private/var, /tmp -> /private/tmp).
    normalized_path = re.sub(r"^/private/var/", "/var/", absolute_path)
    normalized_path = re.sub(r"^/private/tmp(/|$)", r"/tmp\1", normalized_path)
    normalized_working_path = re.sub(r"^/private/var/", "/var/", absolute_working_path)
    normalized_working_path = re.sub(r"^/private/tmp(/|$)", r"/tmp\1", normalized_working_path)

    # Normalize case for case-insensitive comparison.
    case_normalized_path = normalize_case_for_comparison(normalized_path)
    case_normalized_working_path = normalize_case_for_comparison(normalized_working_path)

    # Use cross-platform relative path helper.
    relative = relative_path(case_normalized_working_path, case_normalized_path)

    # Same path.
    if relative == "":
        return True

    if contains_path_traversal(relative):
        return False

    # Path is inside (relative path that doesn't go up).
    return not posixpath.isabs(relative)


def _get_settings_root_path_for_source(source: PermissionRuleSource) -> str:
    """Settings root directory for a permission-rule ``source`` (without ``.tabvis/``).

    userSettings → the config home; project/local/policy → the original cwd; flagSettings → the
    cwd (no separate flag-settings path in this build).
    """
    if source == "userSettings":
        return os.path.realpath(get_tabvis_config_home_dir())
    # policySettings / projectSettings / localSettings / flagSettings -> original cwd.
    return os.path.realpath(get_original_cwd())


def _root_path_for_source(source: PermissionRuleSource) -> str:
    if source in ("cliArg", "command", "session"):
        return expand_path(get_original_cwd())
    # userSettings / policySettings / projectSettings / localSettings / flagSettings.
    return _get_settings_root_path_for_source(source)


def _prepend_dir_sep(path: str) -> str:
    return posixpath.join(DIR_SEP, path)


def _normalize_pattern_to_path(pattern_root: str, pattern: str, root_path: str) -> str | None:
    """If ``pattern_root + pattern`` falls under ``root_path``, return the path-relative pattern
    (DIR_SEP-prefixed); else ``None`` (pattern outside the reference root, skipped)."""
    full_pattern = posixpath.join(pattern_root, pattern)
    if pattern_root == root_path:
        # Pattern root exactly matches the reference root: no change needed.
        return _prepend_dir_sep(pattern)
    if full_pattern.startswith(f"{root_path}{DIR_SEP}"):
        # Extract the relative part.
        relative_part = full_pattern[len(root_path) :]
        return _prepend_dir_sep(relative_part)
    # Pattern is inside the reference root but doesn't start with it.
    relative = posixpath.relpath(pattern_root, root_path)
    if not relative or relative.startswith(f"..{DIR_SEP}") or relative == "..":
        # Pattern is outside the reference root, so it can be skipped.
        return None
    relative_pattern = posixpath.join(relative, pattern)
    return _prepend_dir_sep(relative_pattern)


def normalize_patterns_to_path(
    patterns_by_root: dict[str | None, list[str]],
    root: str,
) -> list[str]:
    """Resolve every ``{root: [pattern]}`` entry against ``root``; null-root patterns match
    anywhere (carried through unchanged)."""
    # null root means the pattern can match anywhere.
    result: list[str] = list(patterns_by_root.get(None, []))
    seen: set[str] = set(result)

    for pattern_root, patterns in patterns_by_root.items():
        if pattern_root is None:
            continue  # already added
        for pattern in patterns:
            normalized_pattern = _normalize_pattern_to_path(pattern_root, pattern, root)
            if normalized_pattern is not None and normalized_pattern not in seen:
                seen.add(normalized_pattern)
                result.append(normalized_pattern)
    return result


def get_file_read_ignore_patterns(
    tool_permission_context: ToolPermissionContext,
) -> dict[str | None, list[str]]:
    """Collect all Read deny-rule ignore patterns keyed by their root (null = no root).

    Used to hide files blocked by Read deny rules.
    """
    patterns_by_root = _get_patterns_by_root(tool_permission_context, "read", "deny")
    result: dict[str | None, list[str]] = {}
    for pattern_root, pattern_map in patterns_by_root.items():
        result[pattern_root] = list(pattern_map.keys())
    return result


def _pattern_with_root(pattern: str, source: PermissionRuleSource) -> dict[str, Any]:
    """Split a permission pattern into ``{"relativePattern", "root"}`` (root may be ``None``)."""
    if pattern.startswith(f"{DIR_SEP}{DIR_SEP}"):
        # Patterns starting with // resolve relative to /.
        pattern_without_double_slash = pattern[1:]

        # On Windows, a POSIX-style drive path like //c/Users/...
        if get_platform() == "windows":
            import re

            if re.match(r"^/[a-z]/", pattern_without_double_slash, re.IGNORECASE):
                drive_letter = (
                    pattern_without_double_slash[1].upper()
                    if len(pattern_without_double_slash) > 1
                    else "C"
                )
                path_after_drive = pattern_without_double_slash[2:]
                drive_root = f"{drive_letter}:\\"
                relative_from_drive = (
                    path_after_drive[1:]
                    if path_after_drive.startswith("/")
                    else path_after_drive
                )
                return {"relativePattern": relative_from_drive, "root": drive_root}

        return {"relativePattern": pattern_without_double_slash, "root": DIR_SEP}

    if pattern.startswith(f"~{DIR_SEP}"):
        # Patterns starting with ~/ resolve relative to homedir.
        return {
            "relativePattern": pattern[1:],
            "root": unicodedata.normalize("NFC", os.path.expanduser("~")),
        }

    if pattern.startswith(DIR_SEP):
        # Patterns starting with / resolve relative to the settings root (without .tabvis/).
        return {"relativePattern": pattern, "root": _root_path_for_source(source)}

    # No root specified; normalize a leading "./" so "./.env" matches ".env".
    normalized_pattern = pattern
    if pattern.startswith(f".{DIR_SEP}"):
        normalized_pattern = pattern[2:]
    return {"relativePattern": normalized_pattern, "root": None}


def _get_rule_by_contents_for_tool_name(
    context: ToolPermissionContext,
    tool_name: str,
    behavior: str,
) -> dict[str, PermissionRule]:
    """Map rule-content → rule for a tool name at a behavior.

    Inlined because ``permissions.py`` exposes the source-ordered rule extractors
    (``get_allow_rules`` / ``get_ask_rules`` / ``get_deny_rules``) but not this contents-map helper.
    """
    if behavior == "allow":
        rules = get_allow_rules(context)
    elif behavior == "deny":
        rules = get_deny_rules(context)
    else:
        rules = get_ask_rules(context)

    rule_by_contents: dict[str, PermissionRule] = {}
    for rule in rules:
        rule_value = rule.get("ruleValue", {})
        rule_content = rule_value.get("ruleContent")
        if (
            rule_value.get("toolName") == tool_name
            and rule_content is not None
            and rule.get("ruleBehavior") == behavior
        ):
            rule_by_contents[rule_content] = rule
    return rule_by_contents


def _get_patterns_by_root(
    tool_permission_context: ToolPermissionContext,
    tool_type: str,
    behavior: str,
) -> dict[str | None, dict[str, PermissionRule]]:
    """Group a tool/behavior's rules by their resolved root: ``{root: {relativePattern: rule}}``."""
    if tool_type == "edit":
        # Apply Edit tool rules to any tool editing files.
        tool_name = FILE_EDIT_TOOL_NAME
    else:
        # Apply Read tool rules to any tool reading files.
        tool_name = FILE_READ_TOOL_NAME

    rules = _get_rule_by_contents_for_tool_name(tool_permission_context, tool_name, behavior)
    patterns_by_root: dict[str | None, dict[str, PermissionRule]] = {}
    for pattern, rule in rules.items():
        with_root = _pattern_with_root(pattern, rule["source"])
        relative_pattern = with_root["relativePattern"]
        root = with_root["root"]
        patterns_for_root = patterns_by_root.get(root)
        if patterns_for_root is None:
            patterns_for_root = {}
            patterns_by_root[root] = patterns_for_root
        patterns_for_root[relative_pattern] = rule
    return patterns_by_root


def matching_rule_for_input(
    path: str,
    tool_permission_context: ToolPermissionContext,
    tool_type: str,
    behavior: str,
) -> PermissionRule | None:
    """Return the permission rule matching ``path`` for ``tool_type``/``behavior``, or ``None``.

    Mirrors the TS ``ignore().add(patterns).test(relativePath)`` flow: gitignore-style matching of
    the path (relative to each rule root) against the rule patterns, mapping the winning pattern
    back to its originating rule.
    """
    file_absolute_path = expand_path(path)

    # On Windows, convert to POSIX format to match against permission patterns.
    if get_platform() == "windows" and "\\" in file_absolute_path:
        file_absolute_path = windows_path_to_posix_path(file_absolute_path)

    patterns_by_root = _get_patterns_by_root(tool_permission_context, tool_type, behavior)

    # Check each root for a matching pattern.
    for root, pattern_map in patterns_by_root.items():
        # Transform patterns for the ignore library: drop a trailing /** since gitignore treats
        # 'path' as matching both the path itself and everything inside it.
        adjusted_to_original: dict[str, str] = {}
        adjusted_patterns: list[str] = []
        for pattern in pattern_map:
            adjusted_pattern = pattern
            if adjusted_pattern.endswith("/**"):
                adjusted_pattern = adjusted_pattern[:-3]
            adjusted_patterns.append(adjusted_pattern)
            adjusted_to_original[adjusted_pattern] = pattern

        # Use cross-platform relative path helper for POSIX-style patterns.
        relative_path_str = relative_path(root or get_cwd(), file_absolute_path or get_cwd())

        if relative_path_str.startswith(f"..{DIR_SEP}"):
            # The path is outside the root, so ignore it.
            continue

        # ig.test throws if you give it an empty string.
        if not relative_path_str:
            continue

        matched_adjusted = _gitignore_last_match(adjusted_patterns, relative_path_str)
        if matched_adjusted is None:
            continue

        original_pattern = adjusted_to_original[matched_adjusted]
        return pattern_map.get(original_pattern)

    # No matching rule found.
    return None


def _gitignore_last_match(patterns: list[str], rel_path: str) -> str | None:
    """Return the last pattern in ``patterns`` that ignores ``rel_path`` (gitignore last-match-wins
    semantics), respecting negation (``!pattern``). Returns ``None`` if not ignored.

    The npm ``ignore`` library applies patterns in order, the last matching one deciding; this
    reproduces that and surfaces the deciding pattern so the caller can map it back to a rule.
    ``GitIgnoreSpecPattern`` parses ``!`` natively (``pattern.include is False`` for negation).
    """
    matched: str | None = None
    for pattern in patterns:
        if not pattern or pattern.startswith("#"):
            continue
        try:
            gp = GitIgnoreSpecPattern(pattern)
        except Exception:  # noqa: BLE001 - skip un-compilable patterns (parity with ignore())
            continue
        # include is None for a pattern that compiled to nothing (e.g. a bare '!').
        if gp.include is None:
            continue
        if gp.match_file(rel_path):
            matched = pattern if gp.include else None
    return matched


def check_read_permission_for_tool(
    tool: Tool,
    input: dict[str, Any],
    tool_permission_context: ToolPermissionContext,
) -> PermissionDecision:
    """Permission decision for read permission for ``tool`` + ``input``."""
    path = tool.get_path(input)
    if path is None:
        return {
            "behavior": "ask",
            "message": (
                f"Tabvis requested permissions to use {tool.name}, but you haven't granted it yet."
            ),
        }

    # Paths to check (original + resolved symlinks), computed once and threaded through.
    paths_to_check = get_paths_for_permission_check(path)

    # 1. Defense-in-depth: block UNC paths early (paths starting with \\ or //).
    for path_to_check in paths_to_check:
        if path_to_check.startswith("\\\\") or path_to_check.startswith("//"):
            return {
                "behavior": "ask",
                "message": (
                    f"Tabvis requested permissions to read from {path}, which appears to be a UNC "
                    "path that could access network resources."
                ),
                "decisionReason": {
                    "type": "other",
                    "reason": "UNC path detected (defense-in-depth check)",
                },
            }

    # 2. Suspicious Windows path patterns (defense in depth).
    for path_to_check in paths_to_check:
        if _has_suspicious_windows_path_pattern(path_to_check):
            return {
                "behavior": "ask",
                "message": (
                    f"Tabvis requested permissions to read from {path}, which contains a suspicious "
                    "Windows path pattern that requires manual approval."
                ),
                "decisionReason": {
                    "type": "other",
                    "reason": (
                        "Path contains suspicious Windows-specific patterns (alternate data "
                        "streams, short names, long path prefixes, or three or more consecutive "
                        "dots) that require manual verification"
                    ),
                },
            }

    # 3. READ-SPECIFIC deny rules first (both original + resolved symlink path). SECURITY: before
    # any allow check (including edit-implies-read) to prevent bypassing explicit read deny rules.
    for path_to_check in paths_to_check:
        deny_rule = matching_rule_for_input(
            path_to_check, tool_permission_context, "read", "deny"
        )
        if deny_rule:
            return {
                "behavior": "deny",
                "message": f"Permission to read {path} has been denied.",
                "decisionReason": {"type": "rule", "rule": deny_rule},
            }

    # 4. READ-SPECIFIC ask rules (before implicit allow checks).
    for path_to_check in paths_to_check:
        ask_rule = matching_rule_for_input(path_to_check, tool_permission_context, "read", "ask")
        if ask_rule:
            return {
                "behavior": "ask",
                "message": (
                    f"Tabvis requested permissions to read from {path}, but you haven't granted it "
                    "yet."
                ),
                "decisionReason": {"type": "rule", "rule": ask_rule},
            }

    # 5. Edit access implies read access (only after read-specific deny/ask rules).
    edit_result = check_write_permission_for_tool(
        tool, input, tool_permission_context, paths_to_check
    )
    if edit_result["behavior"] == "allow":
        return edit_result

    # 6. Allow reads in working directories.
    is_in_working_dir = path_in_allowed_working_path(path, tool_permission_context, paths_to_check)
    if is_in_working_dir:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "mode", "mode": "default"},
        }

    # 7. Allow reads from internal harness paths (session-memory, plans, tool-results).
    absolute_path = expand_path(path)
    internal_read_result = check_readable_internal_path(absolute_path, input)
    if internal_read_result["behavior"] != "passthrough":
        return internal_read_result

    # 8. Check for allow rules.
    allow_rule = matching_rule_for_input(path, tool_permission_context, "read", "allow")
    if allow_rule:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "rule", "rule": allow_rule},
        }

    # 12. Default to asking for permission (path is outside working directories at this point).
    return {
        "behavior": "ask",
        "message": (
            f"Tabvis requested permissions to read from {path}, but you haven't granted it yet."
        ),
        "suggestions": generate_suggestions(
            path, "read", tool_permission_context, paths_to_check
        ),
        "decisionReason": {
            "type": "workingDir",
            "reason": "Path is outside allowed working directories",
        },
    }


def check_write_permission_for_tool(
    tool: Tool,
    input: dict[str, Any],
    tool_permission_context: ToolPermissionContext,
    precomputed_paths_to_check: list[str] | tuple[str, ...] | None = None,
) -> PermissionDecision:
    """Permission decision for write permission for ``tool`` + ``input``.

    ``precomputed_paths_to_check`` is an optional cached
    ``get_paths_for_permission_check(tool.get_path(input))`` — callers MUST derive it from the same
    ``tool``/``input`` in the same synchronous frame (``path`` is re-derived internally).
    """
    path = tool.get_path(input)
    if path is None:
        return {
            "behavior": "ask",
            "message": (
                f"Tabvis requested permissions to use {tool.name}, but you haven't granted it yet."
            ),
        }

    # 1. Deny rules (both original + resolved symlink path).
    paths_to_check = (
        precomputed_paths_to_check
        if precomputed_paths_to_check is not None
        else get_paths_for_permission_check(path)
    )
    for path_to_check in paths_to_check:
        deny_rule = matching_rule_for_input(
            path_to_check, tool_permission_context, "edit", "deny"
        )
        if deny_rule:
            return {
                "behavior": "deny",
                "message": f"Permission to edit {path} has been denied.",
                "decisionReason": {"type": "rule", "rule": deny_rule},
            }

    # 1.5. Allow writes to internal editable paths (plan files, scratchpad). MUST come before
    # _is_dangerous_file_path_to_auto_edit since .tabvis is a dangerous directory.
    absolute_path_for_edit = expand_path(path)
    internal_edit_result = check_editable_internal_path(absolute_path_for_edit, input)
    if internal_edit_result["behavior"] != "passthrough":
        return internal_edit_result

    # 1.6. Check for .tabvis/** allow rules BEFORE safety checks (session-level rules only, to avoid
    # permanently granting broad .tabvis/ access). Scope the search to session-only rules.
    session_rules = tool_permission_context.get("alwaysAllowRules", {}).get("session") or []
    scoped_context: ToolPermissionContext = {
        **tool_permission_context,
        "alwaysAllowRules": {"session": session_rules},
    }
    tabvis_folder_allow_rule = matching_rule_for_input(path, scoped_context, "edit", "allow")
    if tabvis_folder_allow_rule:
        # Accept the broad patterns ('/.tabvis/**', '~/.tabvis/**') and narrowed ones like
        # '/.tabvis/skills/my-skill/**'. Reject '..'.
        rule_content = tabvis_folder_allow_rule.get("ruleValue", {}).get("ruleContent")
        if (
            rule_content
            and (
                rule_content.startswith(TABVIS_FOLDER_PERMISSION_PATTERN[:-2])
                or rule_content.startswith(GLOBAL_TABVIS_FOLDER_PERMISSION_PATTERN[:-2])
            )
            and ".." not in rule_content
            and rule_content.endswith("/**")
        ):
            return {
                "behavior": "allow",
                "updatedInput": input,
                "decisionReason": {"type": "rule", "rule": tabvis_folder_allow_rule},
            }

    # 1.7. Comprehensive safety validations (Windows patterns, Tabvis config, dangerous files). MUST
    # come before allow rules so users can't accidentally grant permission to edit protected files.
    safety_check = check_path_safety_for_auto_edit(path, paths_to_check)
    if not safety_check["safe"]:
        # SDK suggestion: if under .tabvis/skills/{name}/, emit the narrowed session-scoped addRules
        # that step 1.6 will honor next call. Everything else falls back to generate_suggestions.
        skill_scope = get_tabvis_skill_scope(path)
        if skill_scope:
            safety_suggestions: list[PermissionUpdate] = [
                {
                    "type": "addRules",
                    "rules": [
                        {
                            "toolName": FILE_EDIT_TOOL_NAME,
                            "ruleContent": skill_scope["pattern"],
                        },
                    ],
                    "behavior": "allow",
                    "destination": "session",
                },
            ]
        else:
            safety_suggestions = generate_suggestions(
                path, "write", tool_permission_context, paths_to_check
            )
        return {
            "behavior": "ask",
            "message": safety_check["message"],
            "suggestions": safety_suggestions,
            "decisionReason": {"type": "safetyCheck", "reason": safety_check["message"]},
        }

    # 2. Ask rules (both original + resolved symlink path).
    for path_to_check in paths_to_check:
        ask_rule = matching_rule_for_input(path_to_check, tool_permission_context, "edit", "ask")
        if ask_rule:
            return {
                "behavior": "ask",
                "message": (
                    f"Tabvis requested permissions to write to {path}, but you haven't granted it "
                    "yet."
                ),
                "decisionReason": {"type": "rule", "rule": ask_rule},
            }

    # 3. In acceptEdits mode, allow all writes in original cwd.
    is_in_working_dir = path_in_allowed_working_path(path, tool_permission_context, paths_to_check)
    if tool_permission_context.get("mode") == "acceptEdits" and is_in_working_dir:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "mode", "mode": tool_permission_context["mode"]},
        }

    # 4. Allow rules.
    allow_rule = matching_rule_for_input(path, tool_permission_context, "edit", "allow")
    if allow_rule:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "rule", "rule": allow_rule},
        }

    # 5. Default to asking for permission.
    decision: PermissionDecision = {
        "behavior": "ask",
        "message": (
            f"Tabvis requested permissions to write to {path}, but you haven't granted it yet."
        ),
        "suggestions": generate_suggestions(
            path, "write", tool_permission_context, paths_to_check
        ),
    }
    if not is_in_working_dir:
        decision["decisionReason"] = {
            "type": "workingDir",
            "reason": "Path is outside allowed working directories",
        }
    return decision


def generate_suggestions(
    file_path: str,
    operation_type: str,
    tool_permission_context: ToolPermissionContext,
    precomputed_paths_to_check: list[str] | tuple[str, ...] | None = None,
) -> list[PermissionUpdate]:
    """Build permission-update suggestions for a denied/asked file operation."""
    # Function-local import breaks the filesystem <-> permission_update cycle.
    from tabvis.utils.permissions.permission_update import create_read_rule_suggestion

    is_outside_working_dir = not path_in_allowed_working_path(
        file_path, tool_permission_context, precomputed_paths_to_check
    )

    if operation_type == "read" and is_outside_working_dir:
        # For read operations outside working dirs, add Read rules. Include both the symlink path
        # and resolved path so subsequent checks pass.
        dir_path = get_directory_for_path(file_path)
        dirs_to_add = get_paths_for_permission_check(dir_path)

        suggestions = [
            s
            for s in (create_read_rule_suggestion(d, "session") for d in dirs_to_add)
            if s is not None
        ]
        return suggestions

    # Only suggest setMode:acceptEdits when it would be an upgrade.
    should_suggest_accept_edits = tool_permission_context.get("mode") in ("default", "plan")

    if operation_type in ("write", "create"):
        updates: list[PermissionUpdate] = (
            [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
            if should_suggest_accept_edits
            else []
        )

        if is_outside_working_dir:
            # Also add the directory. Include both the symlink path and resolved path.
            dir_path = get_directory_for_path(file_path)
            dirs_to_add = get_paths_for_permission_check(dir_path)
            updates.append(
                {
                    "type": "addDirectories",
                    "directories": dirs_to_add,
                    "destination": "session",
                }
            )

        return updates

    # For read operations inside working directories, just change mode.
    return (
        [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
        if should_suggest_accept_edits
        else []
    )


def check_editable_internal_path(
    absolute_path: str,
    input: dict[str, Any],
) -> PermissionResult:
    """Whether ``absolute_path`` is an internal path editable without permission.

    Returns an ``allow`` :data:`PermissionResult` if matched, else ``passthrough``.
    """
    # SECURITY: Normalize path to prevent traversal bypasses via .. segments (defense-in-depth).
    normalized_path = os.path.normpath(absolute_path)

    # Plan files for the current session.
    if _is_session_plan_file(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Plan files for current session are allowed for writing",
            },
        }

    # Scratchpad directory for the current session.
    if _is_scratchpad_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Scratchpad files for current session are allowed for writing",
            },
        }

    # Agent memory directory (for self-improving agents).
    if _is_agent_memory_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Agent memory files are allowed for writing",
            },
        }

    # Memdir directory (persistent memory). The default path is under ~/.tabvis/ (a dangerous dir);
    # the TABVIS_MEMORY_PATH_OVERRIDE override is an arbitrary caller dir with no such conflict, so it
    # gets NO special permission treatment here (goes through normal flow → ask).
    if not has_auto_mem_path_override() and _is_auto_mem_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "auto memory files are allowed for writing",
            },
        }

    # .tabvis/launch.json — desktop preview config (project-level .tabvis/ only).
    if normalize_case_for_comparison(normalized_path) == normalize_case_for_comparison(
        os.path.join(get_original_cwd(), ".tabvis", "launch.json")
    ):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Preview launch config is allowed for writing",
            },
        }

    return {"behavior": "passthrough", "message": ""}


def check_readable_internal_path(
    absolute_path: str,
    input: dict[str, Any],
) -> PermissionResult:
    """Whether ``absolute_path`` is an internal path readable without permission.

    Returns an ``allow`` :data:`PermissionResult` if matched, else ``passthrough``.
    """
    # SECURITY: Normalize path to prevent traversal bypasses via .. segments (defense-in-depth).
    normalized_path = os.path.normpath(absolute_path)

    # Project directory (for reading past session memories).
    if _is_project_dir_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Project directory files are allowed for reading",
            },
        }

    # Plan files for the current session.
    if _is_session_plan_file(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Plan files for current session are allowed for reading",
            },
        }

    # Tool results directory (persisted large outputs). Use a path-separator suffix to prevent
    # traversal (e.g. tool-results-evil/).
    tool_results_dir = get_tool_results_dir()
    tool_results_dir_with_sep = (
        tool_results_dir if tool_results_dir.endswith(os.sep) else tool_results_dir + os.sep
    )
    if normalized_path == tool_results_dir or normalized_path.startswith(tool_results_dir_with_sep):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Tool result files are allowed for reading",
            },
        }

    # Scratchpad directory for the current session.
    if _is_scratchpad_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Scratchpad files for current session are allowed for reading",
            },
        }

    # Project temp directory (/tmp/tabvis/{sanitized-cwd}/). Intentionally allows reading files from
    # all sessions in this project (cross-session access within the same project's temp space).
    project_temp_dir = get_project_temp_dir()
    if normalized_path.startswith(project_temp_dir):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Project temp directory files are allowed for reading",
            },
        }

    # Agent memory directory (for self-improving agents).
    if _is_agent_memory_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Agent memory files are allowed for reading",
            },
        }

    # Memdir directory (persistent memory for cross-session learning).
    if _is_auto_mem_path(normalized_path):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "auto memory files are allowed for reading",
            },
        }

    # Tasks directory (~/.tabvis/tasks/) for swarm task coordination.
    tasks_dir = os.path.join(get_tabvis_config_home_dir(), "tasks") + os.sep
    if normalized_path == tasks_dir[:-1] or normalized_path.startswith(tasks_dir):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Task files are allowed for reading",
            },
        }

    # Teams directory (~/.tabvis/teams/) for swarm coordination.
    teams_read_dir = os.path.join(get_tabvis_config_home_dir(), "teams") + os.sep
    if normalized_path == teams_read_dir[:-1] or normalized_path.startswith(teams_read_dir):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "other",
                "reason": "Team files are allowed for reading",
            },
        }

    return {"behavior": "passthrough", "message": ""}
