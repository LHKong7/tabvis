"""Permission-layer path validation

This is the **permission-layer** path validator (NOT the BashTool ``pathValidation``). It is the
filesystem permission gate used by file/glob/removal operations: tilde expansion, glob-base
extraction, sandbox write-allowlist checks, removal-danger detection, and the central
``is_path_allowed`` rule-precedence ladder (deny → internal-editable → safety → working-dir →
internal-readable → sandbox-allowlist → allow rules → deny).

Lands additively — nothing imports it yet.

Casing: Python identifiers are snake_case; the result dicts that round-trip as permission decisions
keep their camelCase wire keys (``allowed``, ``decisionReason``, ``resolvedPath``) and reuse the
camelCase ``PermissionDecisionReason`` / ``PermissionResult`` TypedDict shapes from the existing
permissions layer (``type``/``rule``/``reason``/``behavior``/``decisionReason``).
"""

from __future__ import annotations

import functools
import os
import posixpath
import re
import sys

from tabvis.types.permissions import (
    PermissionDecisionReason,
    ToolPermissionContext,
)
from tabvis.utils.fs_operations import (
    get_fs_implementation,
    get_paths_for_permission_check,
    safe_resolve_path,
)
from tabvis.utils.path import contains_path_traversal
from tabvis.utils.permissions.filesystem import (
    check_editable_internal_path,
    check_path_safety_for_auto_edit,
    check_readable_internal_path,
    matching_rule_for_input,
    path_in_allowed_working_path,
    path_in_working_path,
)
from tabvis.utils.platform import get_platform
from tabvis.utils.sandbox.sandbox_adapter import SandboxManager
from tabvis.utils.shell.read_only_command_validation import contains_vulnerable_unc_path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DIRS_TO_LIST = 5
# Glob metacharacters: * ? [ ] { }
GLOB_PATTERN_REGEX = re.compile(r"[*?[\]{}]")

# FileOperationType = 'read' | 'write' | 'create' (validated by callers; kept as plain str here).

WINDOWS_DRIVE_ROOT_REGEX = re.compile(r"^[A-Za-z]:/?$")
WINDOWS_DRIVE_CHILD_REGEX = re.compile(r"^[A-Za-z]:/[^/]+$")


# ---------------------------------------------------------------------------
# Result shapes.
#
# The TS exports ``PathCheckResult`` / ``ResolvedPathCheckResult`` as plain object types. The
# Python implementation returns plain dicts with the same camelCase wire keys:
#   PathCheckResult          = {"allowed": bool, "decisionReason"?: PermissionDecisionReason}
#   ResolvedPathCheckResult  = PathCheckResult + {"resolvedPath": str}
# ---------------------------------------------------------------------------


def _homedir() -> str:
    """``os.homedir()`` equivalent."""
    return os.path.expanduser("~")


def format_directory_list(directories: list[str]) -> str:
    """Quote dirs, eliding past ``MAX_DIRS_TO_LIST``."""
    dir_count = len(directories)

    if dir_count <= MAX_DIRS_TO_LIST:
        return ", ".join(f"'{d}'" for d in directories)

    first_dirs = ", ".join(f"'{d}'" for d in directories[:MAX_DIRS_TO_LIST])
    return f"{first_dirs}, and {dir_count - MAX_DIRS_TO_LIST} more"


def get_glob_base_directory(path: str) -> str:
    """Base dir of a glob pattern.

    For example: ``/path/to/*.txt`` -> ``/path/to``.
    """
    glob_match = GLOB_PATTERN_REGEX.search(path)
    if glob_match is None:
        return path

    # Everything before the first glob character.
    before_glob = path[: glob_match.start()]

    # Find the last directory separator.
    if get_platform() == "windows":
        last_sep_index = max(before_glob.rfind("/"), before_glob.rfind("\\"))
    else:
        last_sep_index = before_glob.rfind("/")
    if last_sep_index == -1:
        return "."

    return before_glob[:last_sep_index] or "/"


def expand_tilde(path: str) -> str:
    """Expand a leading ``~`` to ``$HOME``.

    Note: ``~username`` expansion is intentionally NOT supported (security).
    """
    if (
        path == "~"
        or path.startswith("~/")
        or (sys.platform == "win32" and path.startswith("~\\"))
    ):
        return _homedir() + path[1:]
    return path


# Sandbox config paths are session-stable; memoize their resolved forms to avoid repeated
# lstat/realpath syscalls on every write-target check. Matches the getResolvedWorkingDirPaths
# pattern in filesystem.ts. lodash ``memoize`` (default resolver = first arg) → lru_cache keyed
# by the single string argument.
@functools.cache
def _get_resolved_sandbox_config_path(path: str) -> tuple[str, ...]:
    return tuple(get_paths_for_permission_check(path))


def is_path_in_sandbox_write_allowlist(resolved_path: str) -> bool:
    """Sandbox write-allowlist membership.

    When the sandbox is disabled (the unbundled-build default) this short-circuits to ``False``.
    Respects the deny-within-allow list: paths in ``denyWithinAllow`` are blocked even if their
    parent is in ``allowOnly``.
    """
    if not SandboxManager.is_sandboxing_enabled():
        return False
    write_config = SandboxManager.get_fs_write_config()
    if not write_config:
        # No write config (unbundled build) → nothing is in the allowlist.
        return False
    allow_only: list[str] = write_config.get("allowOnly", [])
    deny_within_allow: list[str] = write_config.get("denyWithinAllow", [])

    paths_to_check = get_paths_for_permission_check(resolved_path)
    resolved_allow: list[str] = []
    for a in allow_only:
        resolved_allow.extend(_get_resolved_sandbox_config_path(a))
    resolved_deny: list[str] = []
    for d in deny_within_allow:
        resolved_deny.extend(_get_resolved_sandbox_config_path(d))

    def _ok(p: str) -> bool:
        for deny_path in resolved_deny:
            if path_in_working_path(p, deny_path):
                return False
        return any(path_in_working_path(p, allow_path) for allow_path in resolved_allow)

    return all(_ok(p) for p in paths_to_check)


def is_path_allowed(
    resolved_path: str,
    context: ToolPermissionContext,
    operation_type: str,
    precomputed_paths_to_check: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """The central rule-precedence ladder.

    Returns a ``PathCheckResult`` dict: ``{"allowed": bool, "decisionReason"?: ...}``.
    """
    # Determine which permission type to check based on operation.
    permission_type = "read" if operation_type == "read" else "edit"

    # 1. Check deny rules first (they take precedence).
    deny_rule = matching_rule_for_input(resolved_path, context, permission_type, "deny")
    if deny_rule is not None:
        return {
            "allowed": False,
            "decisionReason": {"type": "rule", "rule": deny_rule},
        }

    # 2. For write/create: internal editable paths (plan files, scratchpad, agent memory, …).
    # MUST come before checkPathSafetyForAutoEdit since .tabvis is a dangerous directory and
    # internal editable paths live under ~/.tabvis/.
    if operation_type != "read":
        internal_edit_result = check_editable_internal_path(resolved_path, {})
        if internal_edit_result.get("behavior") == "allow":
            return {
                "allowed": True,
                "decisionReason": internal_edit_result.get("decisionReason"),
            }

    # 2.5. For write/create: comprehensive safety validations (Windows patterns, Tabvis config
    # files, dangerous files on original + symlink paths). MUST come before working-dir check to
    # prevent bypass via acceptEdits mode.
    if operation_type != "read":
        safety_check = check_path_safety_for_auto_edit(
            resolved_path, precomputed_paths_to_check
        )
        if not safety_check.get("safe"):
            return {
                "allowed": False,
                "decisionReason": {
                    "type": "safetyCheck",
                    "reason": safety_check.get("message"),
                },
            }

    # 3. Allowed working directory. Reads, creates (mkdir/touch), AND in-workdir writes (rm/rmdir/
    # mv/cp, file overwrites) of a path *within* an allowed working directory are auto-allowed here.
    # Operating on a path nested under an allowed working dir is normal, safe work: the step-2.5
    # safety check (tabvis-config / dangerous / sensitive files) already ran above, and the bash layer's
    # dangerous-removal guard (rm of /, /home, …) runs independently — so this does not weaken those.
    # Headless `-p` runs can never reach acceptEdits mode, so without allowing "write" here the model
    # could not even `rm`/overwrite a broken test/<Name>.t.sol it just wrote (one bad test breaks the
    # whole `forge test` build). Paths *outside* the working dir take is_in_working_dir == False and
    # are unaffected (they still require acceptEdits / an allow rule / the sandbox allowlist below).
    is_in_working_dir = path_in_allowed_working_path(
        resolved_path, context, precomputed_paths_to_check
    )
    if is_in_working_dir:
        if (
            operation_type in ("read", "create", "write")
            or context.get("mode") == "acceptEdits"
        ):
            return {"allowed": True}

    # 3.5. For read: internal readable paths (project temp dir, session memory, …).
    if operation_type == "read":
        internal_read_result = check_readable_internal_path(resolved_path, {})
        if internal_read_result.get("behavior") == "allow":
            return {
                "allowed": True,
                "decisionReason": internal_read_result.get("decisionReason"),
            }

    # 3.7. For write/create to paths OUTSIDE the working directory, check the sandbox write
    # allowlist. Paths IN the working directory are intentionally excluded (handled at step 3).
    if (
        operation_type != "read"
        and not is_in_working_dir
        and is_path_in_sandbox_write_allowlist(resolved_path)
    ):
        return {
            "allowed": True,
            "decisionReason": {
                "type": "other",
                "reason": "Path is in sandbox write allowlist",
            },
        }

    # 4. Allow rules for the operation type.
    allow_rule = matching_rule_for_input(resolved_path, context, permission_type, "allow")
    if allow_rule is not None:
        return {
            "allowed": True,
            "decisionReason": {"type": "rule", "rule": allow_rule},
        }

    # 5. Path is not allowed.
    return {"allowed": False}


def validate_glob_pattern(
    clean_path: str,
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    operation_type: str,
) -> dict:
    """Validate a glob via its base directory.

    Returns a ``ResolvedPathCheckResult`` dict.
    """
    if contains_path_traversal(clean_path):
        # For patterns with path traversal, resolve the full path.
        absolute_path = clean_path if os.path.isabs(clean_path) else _resolve(cwd, clean_path)
        resolved = safe_resolve_path(get_fs_implementation(), absolute_path)
        resolved_path = resolved["resolved_path"]
        is_canonical = resolved["is_canonical"]
        result = is_path_allowed(
            resolved_path,
            tool_permission_context,
            operation_type,
            [resolved_path] if is_canonical else None,
        )
        return {
            "allowed": result["allowed"],
            "resolvedPath": resolved_path,
            "decisionReason": result.get("decisionReason"),
        }

    base_path = get_glob_base_directory(clean_path)
    absolute_base_path = base_path if os.path.isabs(base_path) else _resolve(cwd, base_path)
    resolved = safe_resolve_path(get_fs_implementation(), absolute_base_path)
    resolved_path = resolved["resolved_path"]
    is_canonical = resolved["is_canonical"]
    result = is_path_allowed(
        resolved_path,
        tool_permission_context,
        operation_type,
        [resolved_path] if is_canonical else None,
    )
    return {
        "allowed": result["allowed"],
        "resolvedPath": resolved_path,
        "decisionReason": result.get("decisionReason"),
    }


def is_dangerous_removal_path(resolved_path: str) -> bool:
    """Dangerous for rm/rmdir.

    Dangerous paths: ``*``; any path ending ``/*`` or ``\\*``; root ``/``; home ``~``; direct
    children of root (``/usr``, ``/tmp``, …); Windows drive root (``C:\\``) and its direct children.
    """
    # Callers pass both slash forms; collapse runs so C:\\Windows doesn't bypass the drive-child
    # check.
    forward_slashed = re.sub(r"[\\/]+", "/", resolved_path)

    if forward_slashed == "*" or forward_slashed.endswith("/*"):
        return True

    normalized_path = (
        forward_slashed if forward_slashed == "/" else re.sub(r"/$", "", forward_slashed)
    )

    if normalized_path == "/":
        return True

    if WINDOWS_DRIVE_ROOT_REGEX.match(normalized_path):
        return True

    normalized_home = re.sub(r"[\\/]+", "/", _homedir())
    if normalized_path == normalized_home:
        return True

    # Direct children of root: /usr, /tmp, /etc (but not /usr/local).
    parent_dir = posixpath.dirname(normalized_path)
    if parent_dir == "/":
        return True

    if WINDOWS_DRIVE_CHILD_REGEX.match(normalized_path):
        return True

    return False


def validate_path(
    path: str,
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    operation_type: str,
) -> dict:
    """Top-level path validator.

    Handles tilde expansion, UNC/tilde-variant/shell-expansion rejection, glob handling, and path
    resolution. Returns a ``ResolvedPathCheckResult`` dict.
    """
    # Remove surrounding quotes if present.
    clean_path = expand_tilde(re.sub(r"^['\"]|['\"]$", "", path))

    # SECURITY: Block UNC paths that could leak credentials.
    if contains_vulnerable_unc_path(clean_path):
        return {
            "allowed": False,
            "resolvedPath": clean_path,
            "decisionReason": {
                "type": "other",
                "reason": "UNC network paths require manual approval",
            },
        }

    # SECURITY: Reject tilde variants (~user, ~+, ~-, ~N) that expandTilde doesn't handle.
    if clean_path.startswith("~"):
        return {
            "allowed": False,
            "resolvedPath": clean_path,
            "decisionReason": {
                "type": "other",
                "reason": (
                    "Tilde expansion variants (~user, ~+, ~-) in paths require manual approval"
                ),
            },
        }

    # SECURITY: Reject paths containing ANY shell expansion syntax ($ or % characters, or paths
    # starting with = (Zsh equals expansion).
    if "$" in clean_path or "%" in clean_path or clean_path.startswith("="):
        return {
            "allowed": False,
            "resolvedPath": clean_path,
            "decisionReason": {
                "type": "other",
                "reason": "Shell expansion syntax in paths requires manual approval",
            },
        }

    # SECURITY: Block glob patterns in write/create operations.
    if GLOB_PATTERN_REGEX.search(clean_path):
        if operation_type in ("write", "create"):
            return {
                "allowed": False,
                "resolvedPath": clean_path,
                "decisionReason": {
                    "type": "other",
                    "reason": (
                        "Glob patterns are not allowed in write operations. Please specify an "
                        "exact file path."
                    ),
                },
            }

        # For read operations, validate the base directory where the glob would expand.
        return validate_glob_pattern(
            clean_path, cwd, tool_permission_context, operation_type
        )

    # Resolve path.
    absolute_path = clean_path if os.path.isabs(clean_path) else _resolve(cwd, clean_path)
    resolved = safe_resolve_path(get_fs_implementation(), absolute_path)
    resolved_path = resolved["resolved_path"]
    is_canonical = resolved["is_canonical"]

    result = is_path_allowed(
        resolved_path,
        tool_permission_context,
        operation_type,
        [resolved_path] if is_canonical else None,
    )
    return {
        "allowed": result["allowed"],
        "resolvedPath": resolved_path,
        "decisionReason": result.get("decisionReason"),
    }


def _resolve(cwd: str, path: str) -> str:
    """``path.resolve(cwd, path)`` equivalent for a relative ``path`` against ``cwd``."""
    return os.path.abspath(os.path.join(cwd, path))


__all__ = [
    "MAX_DIRS_TO_LIST",
    "GLOB_PATTERN_REGEX",
    "PermissionDecisionReason",
    "expand_tilde",
    "format_directory_list",
    "get_glob_base_directory",
    "is_dangerous_removal_path",
    "is_path_allowed",
    "is_path_in_sandbox_write_allowlist",
    "validate_glob_pattern",
    "validate_path",
]
