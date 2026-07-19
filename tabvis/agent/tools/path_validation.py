"""BashTool path extraction + validation.

Extracts filesystem paths from path-aware shell commands (``cd``/``ls``/``find``/``rm``/
``mv``/``cp``/``grep``/``sed``/``git diff --no-index`` etc.), classifies each command's
operation type, and validates the extracted paths (and output redirections) against the
session's allowed working directories via the permission-layer ``validate_path``.

This is the **BashTool** path validator (NOT ``tabvis.utils.permissions.path_validation``,
the lower-level filesystem permission gate that this delegates to).

Cycle note: ``bash_permissions`` (``strip_safe_wrappers``/``is_normalized_git_command``) is a
cyclic sibling. It is imported **lazily** (function-local) so this module imports standalone
even before ``bash_permissions`` exists on disk. ``tabvis.agent.tools.bash_tool`` is referenced only
for the input type (``Any`` at runtime, ``BashToolInput`` under ``TYPE_CHECKING``); never
top-level imported.

Casing: Python identifiers are snake_case; ``PermissionResult`` / ``decisionReason`` /
``PermissionUpdate`` dicts keep their camelCase wire keys verbatim (``behavior``, ``message``,
``decisionReason``, ``blockedPath``, ``suggestions``, ``addDirectories``, ``setMode`` …).
"""

from __future__ import annotations

import os
import os.path
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tabvis.agent.tools.sed_validation import sed_command_is_allowed_by_allowlist
from tabvis.utils.bash.commands import (
    extract_output_redirections,
    split_command_deprecated,
)
from tabvis.utils.bash.shell_quote import try_parse_shell_command
from tabvis.utils.path import get_directory_for_path
from tabvis.utils.permissions.filesystem import all_working_directories
from tabvis.utils.permissions.path_validation import (
    expand_tilde,
    format_directory_list,
    is_dangerous_removal_path,
    validate_path,
)
from tabvis.utils.permissions.permission_update import create_read_rule_suggestion

if TYPE_CHECKING:
    from tabvis.agent.tools.bash_tool import BashToolInput  # noqa: F401 — type-only
    from tabvis.types.permissions import (  # noqa: F401 — type-only
        PermissionResult,
        ToolPermissionContext,
    )
    from tabvis.utils.bash.ast import Redirect, SimpleCommand  # noqa: F401 — type-only

# Runtime aliases: PermissionResult/ToolPermissionContext are plain dicts (TypedDict unions).
PermissionResult = dict  # noqa: F811 — runtime alias for the TYPE_CHECKING import
# FileOperationType = 'read' | 'write' | 'create' (validated by callers; plain str here).


# ───────────────────────────────────────────────────────────────────────────
# PathCommand universe.
# ───────────────────────────────────────────────────────────────────────────

PathCommand = str  # 'cd' | 'ls' | 'find' | ... (kept as plain str — the keys of PATH_EXTRACTORS)


def _home_dir() -> str:
    return os.path.expanduser("~")


# ───────────────────────────────────────────────────────────────────────────
# Flag/arg extraction helpers.
# ───────────────────────────────────────────────────────────────────────────


def _filter_out_flags(args: list[str]) -> list[str]:
    """Extract positional (non-flag) args, honoring the POSIX ``--`` end-of-options marker.

    After ``--`` ALL args are positional even if they start with ``-`` (security: prevents
    ``rm -- -/../.tabvis/settings.local.json`` from skipping path validation).
    """
    result: list[str] = []
    after_double_dash = False
    for arg in args:
        if after_double_dash:
            result.append(arg)
        elif arg == "--":
            after_double_dash = True
        elif not (arg or "").startswith("-"):
            result.append(arg)
    return result


def _parse_pattern_command(
    args: list[str],
    flags_with_args: set[str],
    defaults: list[str] | None = None,
) -> list[str]:
    """Parse grep/rg/jq-style commands: ``[flags] pattern [paths...]``."""
    if defaults is None:
        defaults = []
    paths: list[str] = []
    pattern_found = False
    after_double_dash = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg is None:
            i += 1
            continue

        if not after_double_dash and arg == "--":
            after_double_dash = True
            i += 1
            continue

        if not after_double_dash and arg.startswith("-"):
            flag = arg.split("=")[0]
            if flag and flag in ("-e", "--regexp", "-f", "--file"):
                pattern_found = True
            if flag and flag in flags_with_args and "=" not in arg:
                i += 1
            i += 1
            continue

        # First non-flag is pattern, rest are paths.
        if not pattern_found:
            pattern_found = True
            i += 1
            continue
        paths.append(arg)
        i += 1

    return paths if len(paths) > 0 else defaults


# ───────────────────────────────────────────────────────────────────────────
# Per-command path extractors.
# ───────────────────────────────────────────────────────────────────────────


def _extract_cd(args: list[str]) -> list[str]:
    return [_home_dir()] if len(args) == 0 else [" ".join(args)]


def _extract_ls(args: list[str]) -> list[str]:
    paths = _filter_out_flags(args)
    return paths if len(paths) > 0 else ["."]


_NEWER_PATTERN_RE = re.compile(r"^-newer[acmBt][acmtB]$")
_FIND_PATH_FLAGS = {
    "-newer", "-anewer", "-cnewer", "-mnewer", "-samefile", "-path",
    "-wholename", "-ilname", "-lname", "-ipath", "-iwholename",
}


def _extract_find(args: list[str]) -> list[str]:
    paths: list[str] = []
    found_non_global_flag = False
    after_double_dash = False

    i = 0
    while i < len(args):
        arg = args[i]
        if not arg:
            i += 1
            continue

        if after_double_dash:
            paths.append(arg)
            i += 1
            continue

        if arg == "--":
            after_double_dash = True
            i += 1
            continue

        if arg.startswith("-"):
            # Global options don't stop collection.
            if arg in ("-H", "-L", "-P"):
                i += 1
                continue
            found_non_global_flag = True
            if arg in _FIND_PATH_FLAGS or _NEWER_PATTERN_RE.match(arg):
                next_arg = args[i + 1] if i + 1 < len(args) else None
                if next_arg:
                    paths.append(next_arg)
                    i += 1  # skip the path we just processed
            i += 1
            continue

        if not found_non_global_flag:
            paths.append(arg)
        i += 1

    return paths if len(paths) > 0 else ["."]


def _extract_tr(args: list[str]) -> list[str]:
    has_delete = any(
        a == "-d" or a == "--delete" or (a.startswith("-") and "d" in a) for a in args
    )
    non_flags = _filter_out_flags(args)
    return non_flags[1:] if has_delete else non_flags[2:]


_GREP_FLAGS = {
    "-e", "--regexp", "-f", "--file", "--exclude", "--include", "--exclude-dir",
    "--include-dir", "-m", "--max-count", "-A", "--after-context", "-B",
    "--before-context", "-C", "--context",
}


def _extract_grep(args: list[str]) -> list[str]:
    paths = _parse_pattern_command(args, _GREP_FLAGS)
    if len(paths) == 0 and any(a in ("-r", "-R", "--recursive") for a in args):
        return ["."]
    return paths


_RG_FLAGS = {
    "-e", "--regexp", "-f", "--file", "-t", "--type", "-T", "--type-not", "-g",
    "--glob", "-m", "--max-count", "--max-depth", "-r", "--replace", "-A",
    "--after-context", "-B", "--before-context", "-C", "--context",
}


def _extract_rg(args: list[str]) -> list[str]:
    return _parse_pattern_command(args, _RG_FLAGS, ["."])


def _extract_sed(args: list[str]) -> list[str]:
    paths: list[str] = []
    skip_next = False
    script_found = False
    after_double_dash = False

    i = 0
    while i < len(args):
        if skip_next:
            skip_next = False
            i += 1
            continue

        arg = args[i]
        if not arg:
            i += 1
            continue

        if not after_double_dash and arg == "--":
            after_double_dash = True
            i += 1
            continue

        if not after_double_dash and arg.startswith("-"):
            if arg in ("-f", "--file"):
                script_file = args[i + 1] if i + 1 < len(args) else None
                if script_file:
                    paths.append(script_file)
                    skip_next = True
                script_found = True
            elif arg in ("-e", "--expression"):
                skip_next = True
                script_found = True
            elif "e" in arg or "f" in arg:
                script_found = True
            i += 1
            continue

        if not script_found:
            script_found = True
            i += 1
            continue

        paths.append(arg)
        i += 1

    return paths


_JQ_FLAGS_WITH_ARGS = {
    "-e", "--expression", "-f", "--from-file", "--arg", "--argjson", "--slurpfile",
    "--rawfile", "--args", "--jsonargs", "-L", "--library-path", "--indent", "--tab",
}


def _extract_jq(args: list[str]) -> list[str]:
    paths: list[str] = []
    filter_found = False
    after_double_dash = False

    i = 0
    while i < len(args):
        arg = args[i]
        if arg is None:
            i += 1
            continue

        if not after_double_dash and arg == "--":
            after_double_dash = True
            i += 1
            continue

        if not after_double_dash and arg.startswith("-"):
            flag = arg.split("=")[0]
            if flag and flag in ("-e", "--expression"):
                filter_found = True
            if flag and flag in _JQ_FLAGS_WITH_ARGS and "=" not in arg:
                i += 1
            i += 1
            continue

        if not filter_found:
            filter_found = True
            i += 1
            continue
        paths.append(arg)
        i += 1

    return paths


def _extract_git(args: list[str]) -> list[str]:
    # git diff --no-index compares two files outside git's control → validate.
    if len(args) >= 1 and args[0] == "diff" and "--no-index" in args:
        file_paths = _filter_out_flags(args[1:])
        return file_paths[:2]
    return []


# Commands that just filter flags.
_SIMPLE_FILTER_COMMANDS = (
    "mkdir", "touch", "rm", "rmdir", "mv", "cp", "cat", "head", "tail", "sort",
    "uniq", "wc", "cut", "paste", "column", "file", "stat", "diff", "awk",
    "strings", "hexdump", "od", "base64", "nl", "sha256sum", "sha1sum", "md5sum",
)

PATH_EXTRACTORS: dict[str, Callable[[list[str]], list[str]]] = {
    "cd": _extract_cd,
    "ls": _extract_ls,
    "find": _extract_find,
    "tr": _extract_tr,
    "grep": _extract_grep,
    "rg": _extract_rg,
    "sed": _extract_sed,
    "jq": _extract_jq,
    "git": _extract_git,
}
for _cmd in _SIMPLE_FILTER_COMMANDS:
    PATH_EXTRACTORS[_cmd] = _filter_out_flags

SUPPORTED_PATH_COMMANDS = list(PATH_EXTRACTORS.keys())


ACTION_VERBS: dict[str, str] = {
    "cd": "change directories to",
    "ls": "list files in",
    "find": "search files in",
    "mkdir": "create directories in",
    "touch": "create or modify files in",
    "rm": "remove files from",
    "rmdir": "remove directories from",
    "mv": "move files to/from",
    "cp": "copy files to/from",
    "cat": "concatenate files from",
    "head": "read the beginning of files from",
    "tail": "read the end of files from",
    "sort": "sort contents of files from",
    "uniq": "filter duplicate lines from files in",
    "wc": "count lines/words/bytes in files from",
    "cut": "extract columns from files in",
    "paste": "merge files from",
    "column": "format files from",
    "tr": "transform text from files in",
    "file": "examine file types in",
    "stat": "read file stats from",
    "diff": "compare files from",
    "awk": "process text from files in",
    "strings": "extract strings from files in",
    "hexdump": "display hex dump of files from",
    "od": "display octal dump of files from",
    "base64": "encode/decode files from",
    "nl": "number lines in files from",
    "grep": "search for patterns in files from",
    "rg": "search for patterns in files from",
    "sed": "edit files in",
    "git": "access files with git from",
    "jq": "process JSON from files in",
    "sha256sum": "compute SHA-256 checksums for files in",
    "sha1sum": "compute SHA-1 checksums for files in",
    "md5sum": "compute MD5 checksums for files in",
}

COMMAND_OPERATION_TYPE: dict[str, str] = {
    "cd": "read",
    "ls": "read",
    "find": "read",
    "mkdir": "create",
    "touch": "create",
    "rm": "write",
    "rmdir": "write",
    "mv": "write",
    "cp": "write",
    "cat": "read",
    "head": "read",
    "tail": "read",
    "sort": "read",
    "uniq": "read",
    "wc": "read",
    "cut": "read",
    "paste": "read",
    "column": "read",
    "tr": "read",
    "file": "read",
    "stat": "read",
    "diff": "read",
    "awk": "read",
    "strings": "read",
    "hexdump": "read",
    "od": "read",
    "base64": "read",
    "nl": "read",
    "grep": "read",
    "rg": "read",
    "sed": "write",
    "git": "read",
    "jq": "read",
    "sha256sum": "read",
    "sha1sum": "read",
    "md5sum": "read",
}


# Command-specific validators that run before path validation. Return True if the
# command is valid, False if it should be rejected (flags could bypass path validation).
def _validator_no_flags(args: list[str]) -> bool:
    return not any((arg or "").startswith("-") for arg in args)


COMMAND_VALIDATOR: dict[str, Callable[[list[str]], bool]] = {
    "mv": _validator_no_flags,
    "cp": _validator_no_flags,
}


_QUOTE_STRIP_RE = re.compile(r"^['\"]|['\"]$")


def check_dangerous_removal_paths(
    command: str,  # 'rm' | 'rmdir'
    args: list[str],
    cwd: str,
) -> dict:
    """Block ``rm``/``rmdir`` targeting critical system directories (``rm -rf /``)."""
    extractor = PATH_EXTRACTORS[command]
    paths = extractor(args)

    for path in paths:
        # Check the path WITHOUT resolving symlinks (e.g. /tmp must be caught even
        # though it's a symlink to /private/tmp on macOS).
        clean_path = expand_tilde(_QUOTE_STRIP_RE.sub("", path))
        absolute_path = clean_path if os.path.isabs(clean_path) else os.path.join(cwd, clean_path)
        absolute_path = os.path.normpath(absolute_path)

        if is_dangerous_removal_path(absolute_path):
            return {
                "behavior": "ask",
                "message": (
                    f"Dangerous {command} operation detected: '{absolute_path}'\n\n"
                    "This command would remove a critical system directory. This requires "
                    "explicit approval and cannot be auto-allowed by permission rules."
                ),
                "decisionReason": {
                    "type": "other",
                    "reason": f"Dangerous {command} operation on critical path: {absolute_path}",
                },
                "suggestions": [],
            }

    return {
        "behavior": "passthrough",
        "message": f"No dangerous removals detected for {command} command",
    }


def _validate_command_paths(
    command: str,
    args: list[str],
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
    operation_type_override: str | None = None,
) -> dict:
    extractor = PATH_EXTRACTORS[command]
    paths = extractor(args)
    operation_type = operation_type_override or COMMAND_OPERATION_TYPE[command]

    validator = COMMAND_VALIDATOR.get(command)
    if validator and not validator(args):
        return {
            "behavior": "ask",
            "message": (
                f"{command} with flags requires manual approval to ensure path safety. "
                f"For security, Tabvis cannot automatically validate {command} commands that use "
                "flags, as some flags like --target-directory=PATH can bypass path validation."
            ),
            "decisionReason": {
                "type": "other",
                "reason": f"{command} command with flags requires manual approval",
            },
        }

    # Block write operations in compound commands containing 'cd' (path-resolution bypass).
    if compound_command_has_cd and operation_type != "read":
        return {
            "behavior": "ask",
            "message": (
                "Commands that change directories and perform write operations require explicit "
                "approval to ensure paths are evaluated correctly. For security, Tabvis cannot "
                "automatically determine the final working directory when 'cd' is used in "
                "compound commands."
            ),
            "decisionReason": {
                "type": "other",
                "reason": (
                    "Compound command contains cd with write operation - manual approval "
                    "required to prevent path resolution bypass"
                ),
            },
        }

    for path in paths:
        result = validate_path(path, cwd, tool_permission_context, operation_type)
        allowed = result["allowed"]
        resolved_path = result["resolvedPath"]
        decision_reason = result.get("decisionReason")

        if not allowed:
            working_dirs = list(all_working_directories(tool_permission_context))
            dir_list_str = format_directory_list(working_dirs)

            reason_type = decision_reason.get("type") if decision_reason else None
            if reason_type in ("other", "safetyCheck"):
                message = decision_reason["reason"]
            else:
                message = (
                    f"{command} in '{resolved_path}' was blocked. For security, Tabvis may only "
                    f"{ACTION_VERBS[command]} the allowed working directories for this session: "
                    f"{dir_list_str}."
                )

            if reason_type == "rule":
                return {
                    "behavior": "deny",
                    "message": message,
                    "decisionReason": decision_reason,
                }

            return {
                "behavior": "ask",
                "message": message,
                "blockedPath": resolved_path,
                "decisionReason": decision_reason,
            }

    return {
        "behavior": "passthrough",
        "message": f"Path validation passed for {command} command",
    }


def create_path_checker(
    command: str,
    operation_type_override: str | None = None,
) -> Callable[..., dict]:
    def _checker(
        args: list[str],
        cwd: str,
        context: ToolPermissionContext,
        compound_command_has_cd: bool | None = None,
    ) -> dict:
        # First check normal path validation (which includes explicit deny rules).
        result = _validate_command_paths(
            command,
            args,
            cwd,
            context,
            compound_command_has_cd,
            operation_type_override,
        )

        # If explicitly denied, respect that.
        if result["behavior"] == "deny":
            return result

        # Check for dangerous removal paths AFTER explicit deny but BEFORE other results.
        if command in ("rm", "rmdir"):
            dangerous_path_result = check_dangerous_removal_paths(command, args, cwd)
            if dangerous_path_result["behavior"] != "passthrough":
                return dangerous_path_result

        if result["behavior"] == "passthrough":
            return result

        if result["behavior"] == "ask":
            operation_type = operation_type_override or COMMAND_OPERATION_TYPE[command]
            suggestions: list[dict] = []

            blocked_path = result.get("blockedPath")
            if blocked_path:
                if operation_type == "read":
                    dir_path = get_directory_for_path(blocked_path)
                    suggestion = create_read_rule_suggestion(dir_path, "session")
                    if suggestion:
                        suggestions.append(suggestion)
                else:
                    suggestions.append({
                        "type": "addDirectories",
                        "directories": [get_directory_for_path(blocked_path)],
                        "destination": "session",
                    })

            if operation_type in ("write", "create"):
                suggestions.append({
                    "type": "setMode",
                    "mode": "acceptEdits",
                    "destination": "session",
                })

            result["suggestions"] = suggestions

        return result

    return _checker


def _parse_command_arguments(cmd: str) -> list[str]:
    """Parse a command with shell-quote, converting glob objects to their pattern strings."""
    parse_result = try_parse_shell_command(cmd, lambda env: f"${env}")
    if not parse_result["success"]:
        return []
    parsed = parse_result["tokens"]
    extracted_args: list[str] = []

    for arg in parsed:
        if isinstance(arg, str):
            extracted_args.append(arg)
        elif (
            isinstance(arg, dict)
            and arg.get("op") == "glob"
            and "pattern" in arg
        ):
            extracted_args.append(str(arg["pattern"]))

    return extracted_args


def _validate_single_path_command(
    cmd: str,
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
) -> dict:
    # Lazy import: bash_permissions is a cyclic sibling.
    from tabvis.agent.tools.bash_permissions import strip_safe_wrappers

    stripped_cmd = strip_safe_wrappers(cmd)

    extracted_args = _parse_command_arguments(stripped_cmd)
    if len(extracted_args) == 0:
        return {"behavior": "passthrough", "message": "Empty command - no paths to validate"}

    base_cmd = extracted_args[0]
    args = extracted_args[1:]
    if not base_cmd or base_cmd not in SUPPORTED_PATH_COMMANDS:
        return {
            "behavior": "passthrough",
            "message": f"Command '{base_cmd}' is not a path-restricted command",
        }

    operation_type_override = (
        "read"
        if base_cmd == "sed" and sed_command_is_allowed_by_allowlist(stripped_cmd)
        else None
    )

    path_checker = create_path_checker(base_cmd, operation_type_override)
    return path_checker(args, cwd, tool_permission_context, compound_command_has_cd)


def _validate_single_path_command_argv(
    cmd: SimpleCommand,
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
) -> dict:
    # Lazy import: bash_permissions is a cyclic sibling.
    from tabvis.agent.tools.bash_permissions import strip_safe_wrappers

    argv = strip_wrappers_from_argv(list(cmd.argv))
    if len(argv) == 0:
        return {"behavior": "passthrough", "message": "Empty command - no paths to validate"}
    base_cmd = argv[0]
    args = argv[1:]
    if not base_cmd or base_cmd not in SUPPORTED_PATH_COMMANDS:
        return {
            "behavior": "passthrough",
            "message": f"Command '{base_cmd}' is not a path-restricted command",
        }
    operation_type_override = (
        "read"
        if base_cmd == "sed"
        and sed_command_is_allowed_by_allowlist(strip_safe_wrappers(cmd.text))
        else None
    )
    path_checker = create_path_checker(base_cmd, operation_type_override)
    return path_checker(args, cwd, tool_permission_context, compound_command_has_cd)


def _validate_output_redirections(
    redirections: list[dict],
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
) -> dict:
    if compound_command_has_cd and len(redirections) > 0:
        return {
            "behavior": "ask",
            "message": (
                "Commands that change directories and write via output redirection require "
                "explicit approval to ensure paths are evaluated correctly. For security, Tabvis "
                "cannot automatically determine the final working directory when 'cd' is used in "
                "compound commands."
            ),
            "decisionReason": {
                "type": "other",
                "reason": (
                    "Compound command contains cd with output redirection - manual approval "
                    "required to prevent path resolution bypass"
                ),
            },
        }
    for redirection in redirections:
        target = redirection["target"]
        # /dev/null is always safe — it discards output.
        if target == "/dev/null":
            continue
        result = validate_path(target, cwd, tool_permission_context, "create")
        allowed = result["allowed"]
        resolved_path = result["resolvedPath"]
        decision_reason = result.get("decisionReason")

        if not allowed:
            working_dirs = list(all_working_directories(tool_permission_context))
            dir_list_str = format_directory_list(working_dirs)

            reason_type = decision_reason.get("type") if decision_reason else None
            if reason_type in ("other", "safetyCheck"):
                message = decision_reason["reason"]
            elif reason_type == "rule":
                message = f"Output redirection to '{resolved_path}' was blocked by a deny rule."
            else:
                message = (
                    f"Output redirection to '{resolved_path}' was blocked. For security, Tabvis "
                    "may only write to files in the allowed working directories for this "
                    f"session: {dir_list_str}."
                )

            if reason_type == "rule":
                return {
                    "behavior": "deny",
                    "message": message,
                    "decisionReason": decision_reason,
                }

            return {
                "behavior": "ask",
                "message": message,
                "blockedPath": resolved_path,
                "decisionReason": decision_reason,
                "suggestions": [
                    {
                        "type": "addDirectories",
                        "directories": [get_directory_for_path(resolved_path)],
                        "destination": "session",
                    }
                ],
            }

    return {"behavior": "passthrough", "message": "No unsafe redirections found"}


_PROCESS_SUBSTITUTION_RE = re.compile(r">>\s*>\s*\(|>\s*>\s*\(|<\s*\(")


def check_path_constraints(
    input: Any,
    cwd: str,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
    ast_redirects: list[Redirect] | None = None,
    ast_commands: list[SimpleCommand] | None = None,
) -> dict:
    """Check path constraints for filesystem commands + validate output redirections.

    Returns a ``PermissionResult`` dict (``ask``/``deny`` to block, ``passthrough`` otherwise).
    """
    command = input.command if not isinstance(input, dict) else input["command"]

    # Process substitution >(cmd)/<(cmd) can write to files without redirect targets.
    if not ast_commands and _PROCESS_SUBSTITUTION_RE.search(command):
        return {
            "behavior": "ask",
            "message": (
                "Process substitution (>(...) or <(...)) can execute arbitrary commands and "
                "requires manual approval"
            ),
            "decisionReason": {
                "type": "other",
                "reason": "Process substitution requires manual approval",
            },
        }

    if ast_redirects is not None:
        redir_result = _ast_redirects_to_output_redirections(ast_redirects)
    else:
        redir_result = extract_output_redirections(command)
    redirections = redir_result["redirections"]
    has_dangerous_redirection = redir_result["hasDangerousRedirection"]

    if has_dangerous_redirection:
        return {
            "behavior": "ask",
            "message": "Shell expansion syntax in paths requires manual approval",
            "decisionReason": {
                "type": "other",
                "reason": "Shell expansion syntax in paths requires manual approval",
            },
        }
    redirection_result = _validate_output_redirections(
        redirections, cwd, tool_permission_context, compound_command_has_cd
    )
    if redirection_result["behavior"] != "passthrough":
        return redirection_result

    if ast_commands is not None:
        for cmd in ast_commands:
            result = _validate_single_path_command_argv(
                cmd, cwd, tool_permission_context, compound_command_has_cd
            )
            if result["behavior"] in ("ask", "deny"):
                return result
    else:
        commands = split_command_deprecated(command)
        for cmd_str in commands:
            result = _validate_single_path_command(
                cmd_str, cwd, tool_permission_context, compound_command_has_cd
            )
            if result["behavior"] in ("ask", "deny"):
                return result

    return {"behavior": "passthrough", "message": "All path commands validated successfully"}


_FD_DUP_DIGITS_RE = re.compile(r"^\d+$")


def _ast_redirects_to_output_redirections(redirects: list[Redirect]) -> dict:
    """Convert AST ``Redirect[]`` to ``{target, operator}`` dicts (output-only)."""
    redirections: list[dict] = []
    for r in redirects:
        op = r.op
        if op in (">", ">|", "&>"):
            redirections.append({"target": r.target, "operator": ">"})
        elif op in (">>", "&>>"):
            redirections.append({"target": r.target, "operator": ">>"})
        elif op == ">&":
            # >&N (digits only) is fd duplication, not a file write.
            if not _FD_DUP_DIGITS_RE.match(r.target):
                redirections.append({"target": r.target, "operator": ">"})
        # '<', '<<', '<&', '<<<' → input redirects, skip.
    return {"redirections": redirections, "hasDangerousRedirection": False}


# ───────────────────────────────────────────────────────────────────────────
# Argv-level safe-wrapper stripping (timeout, nice, stdbuf, env, time, nohup).
# Canonical wrapper-stripping used by path validation. KEEP IN SYNC with the
# text-based strip_safe_wrappers (bash_permissions) and checkSemantics (ast).
# ───────────────────────────────────────────────────────────────────────────

# Allowlist for timeout flag VALUES (signals TERM/KILL/9, durations 5/5s/10.5).
_TIMEOUT_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_TIMEOUT_LONG_FUSED_RE = re.compile(r"^--(?:kill-after|signal)=[A-Za-z0-9_.+-]+$")
_TIMEOUT_SHORT_FUSED_RE = re.compile(r"^-[ks][A-Za-z0-9_.+-]+$")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_NICE_LEVEL_N_RE = re.compile(r"^-?\d+$")
_NICE_LEGACY_RE = re.compile(r"^-\d+$")


def _skip_timeout_flags(a: list[str]) -> int:
    """Parse timeout's flags; return argv index of the DURATION token, or -1 if unparseable."""
    i = 1
    while i < len(a):
        arg = a[i]
        nxt = a[i + 1] if i + 1 < len(a) else None
        if arg in ("--foreground", "--preserve-status", "--verbose"):
            i += 1
        elif _TIMEOUT_LONG_FUSED_RE.match(arg):
            i += 1
        elif arg in ("--kill-after", "--signal") and nxt and _TIMEOUT_FLAG_VALUE_RE.match(nxt):
            i += 2
        elif arg == "--":
            i += 1
            break
        elif arg.startswith("--"):
            return -1
        elif arg == "-v":
            i += 1
        elif arg in ("-k", "-s") and nxt and _TIMEOUT_FLAG_VALUE_RE.match(nxt):
            i += 2
        elif _TIMEOUT_SHORT_FUSED_RE.match(arg):
            i += 1
        elif arg.startswith("-"):
            return -1
        else:
            break
    return i


_STDBUF_SHORT_SEP_RE = re.compile(r"^-[ioe]$")
_STDBUF_SHORT_FUSED_RE = re.compile(r"^-[ioe].")
_STDBUF_LONG_RE = re.compile(r"^--(input|output|error)=")


def _skip_stdbuf_flags(a: list[str]) -> int:
    """Parse stdbuf's flags; return argv index of wrapped COMMAND, or -1 if unparseable/inert."""
    i = 1
    while i < len(a):
        arg = a[i]
        if _STDBUF_SHORT_SEP_RE.match(arg) and i + 1 < len(a) and a[i + 1]:
            i += 2
        elif _STDBUF_SHORT_FUSED_RE.match(arg):
            i += 1
        elif _STDBUF_LONG_RE.match(arg):
            i += 1
        elif arg.startswith("-"):
            return -1  # unknown flag: fail closed
        else:
            break
    return i if i > 1 and i < len(a) else -1


def _skip_env_flags(a: list[str]) -> int:
    """Parse env's VAR=val + safe flags; return argv index of wrapped COMMAND, or -1."""
    i = 1
    while i < len(a):
        arg = a[i]
        if "=" in arg and not arg.startswith("-"):
            i += 1
        elif arg in ("-i", "-0", "-v"):
            i += 1
        elif arg == "-u" and i + 1 < len(a) and a[i + 1]:
            i += 2
        elif arg.startswith("-"):
            return -1  # -S/-C/-P/unknown: fail closed
        else:
            break
    return i if i < len(a) else -1


def strip_wrappers_from_argv(argv: list[str]) -> list[str]:
    """Argv-level counterpart to ``strip_safe_wrappers`` — strip wrapper commands from argv."""
    a = argv
    while True:
        head = a[0] if a else None
        if head in ("time", "nohup"):
            a = a[(2 if len(a) > 1 and a[1] == "--" else 1):]
        elif head == "timeout":
            i = _skip_timeout_flags(a)
            if i < 0 or i >= len(a) or not a[i] or not _TIMEOUT_DURATION_RE.match(a[i]):
                return a
            a = a[i + 1:]
        elif head == "nice":
            if len(a) > 2 and a[1] == "-n" and a[2] and _NICE_LEVEL_N_RE.match(a[2]):
                a = a[(4 if len(a) > 3 and a[3] == "--" else 3):]
            elif len(a) > 1 and a[1] and _NICE_LEGACY_RE.match(a[1]):
                a = a[(3 if len(a) > 2 and a[2] == "--" else 2):]
            else:
                a = a[(2 if len(a) > 1 and a[1] == "--" else 1):]
        elif head == "stdbuf":
            i = _skip_stdbuf_flags(a)
            if i < 0:
                return a
            a = a[i:]
        elif head == "env":
            i = _skip_env_flags(a)
            if i < 0:
                return a
            a = a[i:]
        else:
            return a
