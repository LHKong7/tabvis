"""Shared command validation maps for shell tools (BashTool, PowerShellTool, etc.).

Read the only command validation.

Exports complete command configuration maps that any shell tool can import:
- ``GIT_READ_ONLY_COMMANDS``: all git subcommands with safe flags and callbacks
- ``GH_READ_ONLY_COMMANDS``: tabvis-only gh CLI commands (network-dependent)
- ``EXTERNAL_READONLY_COMMANDS``: cross-shell commands that work in both bash and PowerShell
- ``contains_vulnerable_unc_path``: UNC path detection for credential leak prevention

Wire/data shapes are plain dicts (these are pure-logic lookup tables, not round-tripped
to JSON/SDK/settings), so flag keys keep their literal CLI spellings (``--name-only``,
``-S``, …) verbatim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal, TypedDict

from tabvis.utils.platform import get_platform

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# 'none'   -> No argument (--color, -n)
# 'number' -> Integer argument (--context=3)
# 'string' -> Any string argument (--relative=path)
# 'char'   -> Single character (delimiter)
# '{}'     -> Literal "{}" only
# 'EOF'    -> Literal "EOF" only
FlagArgType = Literal["none", "number", "string", "char", "{}", "EOF"]

# Callback: returns True if the command is dangerous, False if safe.
# ``args`` is the list of tokens AFTER the command name (e.g., after "git branch").
AdditionalCommandIsDangerousCallback = Callable[[str, list[str]], bool]


class ExternalCommandConfig(TypedDict, total=False):
    """Configuration for an external read-only command.

    - ``safeFlags`` (required): map of flag spelling -> FlagArgType.
    - ``additionalCommandIsDangerousCallback``: extra positional/flag check.
    - ``respectsDoubleDash``: when False, the tool does NOT respect POSIX ``--``
      end-of-options; validate_flags keeps checking flags after ``--`` instead of
      breaking. Default: True (most tools respect ``--``).
    """

    safeFlags: dict[str, FlagArgType]
    additionalCommandIsDangerousCallback: AdditionalCommandIsDangerousCallback
    respectsDoubleDash: bool


# ---------------------------------------------------------------------------
# Shared git flag groups
# ---------------------------------------------------------------------------

GIT_REF_SELECTION_FLAGS: dict[str, FlagArgType] = {
    "--all": "none",
    "--branches": "none",
    "--tags": "none",
    "--remotes": "none",
}

GIT_DATE_FILTER_FLAGS: dict[str, FlagArgType] = {
    "--since": "string",
    "--after": "string",
    "--until": "string",
    "--before": "string",
}

GIT_LOG_DISPLAY_FLAGS: dict[str, FlagArgType] = {
    "--oneline": "none",
    "--graph": "none",
    "--decorate": "none",
    "--no-decorate": "none",
    "--date": "string",
    "--relative-date": "none",
}

GIT_COUNT_FLAGS: dict[str, FlagArgType] = {
    "--max-count": "number",
    "-n": "number",
}

# Stat output flags - used in git log, show, diff
GIT_STAT_FLAGS: dict[str, FlagArgType] = {
    "--stat": "none",
    "--numstat": "none",
    "--shortstat": "none",
    "--name-only": "none",
    "--name-status": "none",
}

# Color output flags - used in git log, show, diff
GIT_COLOR_FLAGS: dict[str, FlagArgType] = {
    "--color": "none",
    "--no-color": "none",
}

# Patch display flags - used in git log, show
GIT_PATCH_FLAGS: dict[str, FlagArgType] = {
    "--patch": "none",
    "-p": "none",
    "--no-patch": "none",
    "--no-ext-diff": "none",
    "-s": "none",
}

# Author/committer filter flags - used in git log, reflog
GIT_AUTHOR_FILTER_FLAGS: dict[str, FlagArgType] = {
    "--author": "string",
    "--committer": "string",
    "--grep": "string",
}


# ---------------------------------------------------------------------------
# git reflog / git remote / git tag / git branch dangerous callbacks
# ---------------------------------------------------------------------------


def _git_reflog_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # SECURITY: Block `git reflog expire` (positional subcommand) — it writes
    # to .git/logs/** by expiring reflog entries. `git reflog delete` similarly
    # writes. Only `git reflog` (bare = show) and `git reflog show` are safe.
    #
    # Block known write-capable subcommands: expire, delete, exists.
    # Allow: `show`, ref names (HEAD, refs/*, branch names).
    dangerous_subcommands = {"expire", "delete", "exists"}
    for token in args:
        if not token or token.startswith("-"):
            continue
        # First non-flag positional: check if it's a dangerous subcommand.
        # If it's `show` or a ref name like `HEAD`/`refs/...`, safe.
        if token in dangerous_subcommands:
            return True  # Dangerous subcommand — writes to .git/logs/**
        # First positional is safe (show/HEAD/ref) — subsequent are ref args
        return False
    return False  # No positional = bare `git reflog` = safe (shows reflog)


def _git_remote_show_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # Only allow optional -n, then one alphanumeric remote name.
    positional = [a for a in args if a != "-n"]
    # Must have exactly one positional arg that looks like a remote name
    if len(positional) != 1:
        return True
    return not re.match(r"^[a-zA-Z0-9_-]+$", positional[0])


def _git_remote_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # Only allow bare 'git remote' or 'git remote -v/--verbose'.
    # All args must be known safe flags; no positional args allowed.
    return any(a != "-v" and a != "--verbose" for a in args)


def _git_tag_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # SECURITY: Block tag creation via positional arguments. `git tag foo`
    # creates .git/refs/tags/foo (41-byte file write) — NOT read-only.
    flags_with_args = {
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--sort",
        "--format",
        "-n",
    }
    i = 0
    seen_list_flag = False
    seen_dash_dash = False
    while i < len(args):
        token = args[i]
        if not token:
            i += 1
            continue
        # `--` ends flag parsing. All subsequent tokens are positional args,
        # even if they start with `-`. `git tag -- -l` CREATES a tag named `-l`.
        if token == "--" and not seen_dash_dash:
            seen_dash_dash = True
            i += 1
            continue
        if not seen_dash_dash and token.startswith("-"):
            # Check for -l/--list (exact or in a bundle). `-li` bundles -l and
            # -i — both 'none' type.
            if token == "--list" or token == "-l":
                seen_list_flag = True
            elif (
                token[0] == "-"
                and (len(token) < 2 or token[1] != "-")
                and len(token) > 2
                and "=" not in token
                and "l" in token[1:]
            ):
                # Short-flag bundle like -li, -il containing 'l'
                seen_list_flag = True
            if "=" in token:
                i += 1
            elif token in flags_with_args:
                i += 2
            else:
                i += 1
        else:
            # Non-flag positional arg (or post-`--` positional). Safe only if
            # preceded by -l/--list (then it's a pattern, not a tag name).
            if not seen_list_flag:
                return True  # Positional arg without --list = tag creation
            i += 1
    return False


def _git_branch_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # Block branch creation via positional arguments (e.g., "git branch newbranch").
    # Flags that require an argument.
    flags_with_args = {
        "--contains",
        "--no-contains",
        "--points-at",
        "--sort",
        # --abbrev REMOVED: git does NOT consume detached arg (PARSE_OPT_OPTARG)
    }
    # Flags with optional arguments (don't require, but can take one).
    flags_with_optional_args = {"--merged", "--no-merged"}
    i = 0
    last_flag = ""
    seen_list_flag = False
    seen_dash_dash = False
    while i < len(args):
        token = args[i]
        if not token:
            i += 1
            continue
        # `--` ends flag parsing. `git branch -- -l` CREATES a branch named `-l`.
        if token == "--" and not seen_dash_dash:
            seen_dash_dash = True
            last_flag = ""
            i += 1
            continue
        if not seen_dash_dash and token.startswith("-"):
            # Check for -l/--list including short-flag bundles (-li, -la, etc.)
            if token == "--list" or token == "-l":
                seen_list_flag = True
            elif (
                token[0] == "-"
                and (len(token) < 2 or token[1] != "-")
                and len(token) > 2
                and "=" not in token
                and "l" in token[1:]
            ):
                seen_list_flag = True
            if "=" in token:
                last_flag = token.split("=")[0] or ""
                i += 1
            elif token in flags_with_args:
                last_flag = token
                i += 2
            else:
                last_flag = token
                i += 1
        else:
            # Non-flag argument (or post-`--` positional) - could be:
            # 1. A branch name (dangerous - creates a branch)
            # 2. A pattern after --list/-l (safe)
            # 3. An optional argument after --merged/--no-merged (safe)
            last_flag_has_optional_arg = last_flag in flags_with_optional_args
            if not seen_list_flag and not last_flag_has_optional_arg:
                return True  # Positional arg without --list or filtering flag = branch creation
            i += 1
    return False


# ---------------------------------------------------------------------------
# GIT_READ_ONLY_COMMANDS — complete map of all git subcommands
# ---------------------------------------------------------------------------

GIT_READ_ONLY_COMMANDS: dict[str, ExternalCommandConfig] = {
    "git diff": {
        "safeFlags": {
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            # Display and comparison flags
            "--dirstat": "none",
            "--summary": "none",
            "--patch-with-stat": "none",
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--color-words": "none",
            "--no-renames": "none",
            "--no-ext-diff": "none",
            "--check": "none",
            "--ws-error-highlight": "string",
            "--full-index": "none",
            "--binary": "none",
            "--abbrev": "number",
            "--break-rewrites": "none",
            "--find-renames": "none",
            "--find-copies": "none",
            "--find-copies-harder": "none",
            "--irreversible-delete": "none",
            "--diff-algorithm": "string",
            "--histogram": "none",
            "--patience": "none",
            "--minimal": "none",
            "--ignore-space-at-eol": "none",
            "--ignore-space-change": "none",
            "--ignore-all-space": "none",
            "--ignore-blank-lines": "none",
            "--inter-hunk-context": "number",
            "--function-context": "none",
            "--exit-code": "none",
            "--quiet": "none",
            "--cached": "none",
            "--staged": "none",
            "--pickaxe-regex": "none",
            "--pickaxe-all": "none",
            "--no-index": "none",
            "--relative": "string",
            # Diff filtering
            "--diff-filter": "string",
            # Short flags
            "-p": "none",
            "-u": "none",
            "-s": "none",
            "-M": "none",
            "-C": "none",
            "-B": "none",
            "-D": "none",
            "-l": "none",
            # SECURITY: -S/-G/-O take REQUIRED string arguments (pickaxe search,
            # pickaxe regex, orderfile). 'none' here caused a parser differential
            # with git that enabled ARBITRARY FILE WRITE via `--output`.
            "-S": "string",
            "-G": "string",
            "-O": "string",
            "-R": "none",
        },
    },
    "git log": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
            # Additional display flags
            "--abbrev-commit": "none",
            "--full-history": "none",
            "--dense": "none",
            "--sparse": "none",
            "--simplify-merges": "none",
            "--ancestry-path": "none",
            "--source": "none",
            "--first-parent": "none",
            "--merges": "none",
            "--no-merges": "none",
            "--reverse": "none",
            "--walk-reflogs": "none",
            "--skip": "number",
            "--max-age": "number",
            "--min-age": "number",
            "--no-min-parents": "none",
            "--no-max-parents": "none",
            "--follow": "none",
            # Commit traversal flags
            "--no-walk": "none",
            "--left-right": "none",
            "--cherry-mark": "none",
            "--cherry-pick": "none",
            "--boundary": "none",
            # Ordering flags
            "--topo-order": "none",
            "--date-order": "none",
            "--author-date-order": "none",
            # Format control
            "--pretty": "string",
            "--format": "string",
            # Diff filtering
            "--diff-filter": "string",
            # Pickaxe search (find commits that add/remove string)
            "-S": "string",
            "-G": "string",
            "--pickaxe-regex": "none",
            "--pickaxe-all": "none",
        },
    },
    "git show": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            # Additional display flags
            "--abbrev-commit": "none",
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--color-words": "none",
            "--pretty": "string",
            "--format": "string",
            "--first-parent": "none",
            "--raw": "none",
            # Diff filtering
            "--diff-filter": "string",
            # Short flags
            "-m": "none",
            "--quiet": "none",
        },
    },
    "git shortlog": {
        "safeFlags": {
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            # Summary options
            "-s": "none",
            "--summary": "none",
            "-n": "none",
            "--numbered": "none",
            "-e": "none",
            "--email": "none",
            "-c": "none",
            "--committer": "none",
            # Grouping
            "--group": "string",
            # Formatting
            "--format": "string",
            # Filtering
            "--no-merges": "none",
            "--author": "string",
        },
    },
    "git reflog": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
        },
        "additionalCommandIsDangerousCallback": _git_reflog_is_dangerous,
    },
    "git stash list": {
        "safeFlags": {
            **GIT_LOG_DISPLAY_FLAGS,
            **GIT_REF_SELECTION_FLAGS,
            **GIT_COUNT_FLAGS,
        },
    },
    "git ls-remote": {
        "safeFlags": {
            # Branch/tag filtering flags
            "--branches": "none",
            "-b": "none",
            "--tags": "none",
            "-t": "none",
            "--heads": "none",
            "-h": "none",
            "--refs": "none",
            # Output control flags
            "--quiet": "none",
            "-q": "none",
            "--exit-code": "none",
            "--get-url": "none",
            "--symref": "none",
            # Sorting flags
            "--sort": "string",
            # SECURITY: --server-option and -o are INTENTIONALLY EXCLUDED
            # (network WRITE primitive / exfil).
        },
    },
    "git status": {
        "safeFlags": {
            # Output format flags
            "--short": "none",
            "-s": "none",
            "--branch": "none",
            "-b": "none",
            "--porcelain": "none",
            "--long": "none",
            "--verbose": "none",
            "-v": "none",
            # Untracked files handling
            "--untracked-files": "string",
            "-u": "string",
            # Ignore options
            "--ignored": "none",
            "--ignore-submodules": "string",
            # Column display
            "--column": "none",
            "--no-column": "none",
            # Ahead/behind info
            "--ahead-behind": "none",
            "--no-ahead-behind": "none",
            # Rename detection
            "--renames": "none",
            "--no-renames": "none",
            "--find-renames": "string",
            "-M": "string",
        },
    },
    "git blame": {
        "safeFlags": {
            **GIT_COLOR_FLAGS,
            # Line range
            "-L": "string",
            # Output format
            "--porcelain": "none",
            "-p": "none",
            "--line-porcelain": "none",
            "--incremental": "none",
            "--root": "none",
            "--show-stats": "none",
            "--show-name": "none",
            "--show-number": "none",
            "-n": "none",
            "--show-email": "none",
            "-e": "none",
            "-f": "none",
            # Date formatting
            "--date": "string",
            # Ignore whitespace
            "-w": "none",
            # Ignore revisions
            "--ignore-rev": "string",
            "--ignore-revs-file": "string",
            # Move/copy detection
            "-M": "none",
            "-C": "none",
            "--score-debug": "none",
            # Abbreviation
            "--abbrev": "number",
            # Other options
            "-s": "none",
            "-l": "none",
            "-t": "none",
        },
    },
    "git ls-files": {
        "safeFlags": {
            # File selection
            "--cached": "none",
            "-c": "none",
            "--deleted": "none",
            "-d": "none",
            "--modified": "none",
            "-m": "none",
            "--others": "none",
            "-o": "none",
            "--ignored": "none",
            "-i": "none",
            "--stage": "none",
            "-s": "none",
            "--killed": "none",
            "-k": "none",
            "--unmerged": "none",
            "-u": "none",
            # Output format
            "--directory": "none",
            "--no-empty-directory": "none",
            "--eol": "none",
            "--full-name": "none",
            "--abbrev": "number",
            "--debug": "none",
            "-z": "none",
            "-t": "none",
            "-v": "none",
            "-f": "none",
            # Exclude patterns
            "--exclude": "string",
            "-x": "string",
            "--exclude-from": "string",
            "-X": "string",
            "--exclude-per-directory": "string",
            "--exclude-standard": "none",
            # Error handling
            "--error-unmatch": "none",
            # Recursion
            "--recurse-submodules": "none",
        },
    },
    "git config --get": {
        "safeFlags": {
            # No additional flags needed - just reading config values
            "--local": "none",
            "--global": "none",
            "--system": "none",
            "--worktree": "none",
            "--default": "string",
            "--type": "string",
            "--bool": "none",
            "--int": "none",
            "--bool-or-int": "none",
            "--path": "none",
            "--expiry-date": "none",
            "-z": "none",
            "--null": "none",
            "--name-only": "none",
            "--show-origin": "none",
            "--show-scope": "none",
        },
    },
    # NOTE: 'git remote show' must come BEFORE 'git remote' so longer patterns match first.
    "git remote show": {
        "safeFlags": {
            "-n": "none",
        },
        "additionalCommandIsDangerousCallback": _git_remote_show_is_dangerous,
    },
    "git remote": {
        "safeFlags": {
            "-v": "none",
            "--verbose": "none",
        },
        "additionalCommandIsDangerousCallback": _git_remote_is_dangerous,
    },
    # git merge-base is a read-only command for finding common ancestors
    "git merge-base": {
        "safeFlags": {
            "--is-ancestor": "none",
            "--fork-point": "none",
            "--octopus": "none",
            "--independent": "none",
            "--all": "none",
        },
    },
    # git rev-parse is a pure read command — resolves refs to SHAs, queries repo paths
    "git rev-parse": {
        "safeFlags": {
            # SHA resolution and verification
            "--verify": "none",
            "--short": "string",
            "--abbrev-ref": "none",
            "--symbolic": "none",
            "--symbolic-full-name": "none",
            # Repository path queries (all read-only)
            "--show-toplevel": "none",
            "--show-cdup": "none",
            "--show-prefix": "none",
            "--git-dir": "none",
            "--git-common-dir": "none",
            "--absolute-git-dir": "none",
            "--show-superproject-working-tree": "none",
            # Boolean queries
            "--is-inside-work-tree": "none",
            "--is-inside-git-dir": "none",
            "--is-bare-repository": "none",
            "--is-shallow-repository": "none",
            "--is-shallow-update": "none",
            "--path-prefix": "none",
        },
    },
    # git rev-list is read-only commit enumeration
    "git rev-list": {
        "safeFlags": {
            **GIT_REF_SELECTION_FLAGS,
            **GIT_DATE_FILTER_FLAGS,
            **GIT_COUNT_FLAGS,
            **GIT_AUTHOR_FILTER_FLAGS,
            # Counting
            "--count": "none",
            # Traversal control
            "--reverse": "none",
            "--first-parent": "none",
            "--ancestry-path": "none",
            "--merges": "none",
            "--no-merges": "none",
            "--min-parents": "number",
            "--max-parents": "number",
            "--no-min-parents": "none",
            "--no-max-parents": "none",
            "--skip": "number",
            "--max-age": "number",
            "--min-age": "number",
            "--walk-reflogs": "none",
            # Output formatting
            "--oneline": "none",
            "--abbrev-commit": "none",
            "--pretty": "string",
            "--format": "string",
            "--abbrev": "number",
            "--full-history": "none",
            "--dense": "none",
            "--sparse": "none",
            "--source": "none",
            "--graph": "none",
        },
    },
    # git describe is read-only
    "git describe": {
        "safeFlags": {
            # Tag selection
            "--tags": "none",
            "--match": "string",
            "--exclude": "string",
            # Output control
            "--long": "none",
            "--abbrev": "number",
            "--always": "none",
            "--contains": "none",
            "--first-match": "none",
            "--exact-match": "none",
            "--candidates": "number",
            # Suffix/dirty markers
            "--dirty": "none",
            "--broken": "none",
        },
    },
    # git cat-file is read-only object inspection
    # NOTE: --batch (without --check) is intentionally excluded.
    "git cat-file": {
        "safeFlags": {
            # Object query modes (all purely read-only)
            "-t": "none",
            "-s": "none",
            "-p": "none",
            "-e": "none",
            # Batch mode — read-only check variant only
            "--batch-check": "none",
            # Output control
            "--allow-undetermined-type": "none",
        },
    },
    # git for-each-ref is read-only ref iteration
    "git for-each-ref": {
        "safeFlags": {
            # Output formatting
            "--format": "string",
            # Sorting
            "--sort": "string",
            # Limiting
            "--count": "number",
            # Filtering
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "string",
            "--no-merged": "string",
            "--points-at": "string",
        },
    },
    # git grep is read-only — searches tracked files for patterns
    "git grep": {
        "safeFlags": {
            # Pattern matching modes
            "-e": "string",
            "-E": "none",
            "--extended-regexp": "none",
            "-G": "none",
            "--basic-regexp": "none",
            "-F": "none",
            "--fixed-strings": "none",
            "-P": "none",
            "--perl-regexp": "none",
            # Match control
            "-i": "none",
            "--ignore-case": "none",
            "-v": "none",
            "--invert-match": "none",
            "-w": "none",
            "--word-regexp": "none",
            # Output control
            "-n": "none",
            "--line-number": "none",
            "-c": "none",
            "--count": "none",
            "-l": "none",
            "--files-with-matches": "none",
            "-L": "none",
            "--files-without-match": "none",
            "-h": "none",
            "-H": "none",
            "--heading": "none",
            "--break": "none",
            "--full-name": "none",
            "--color": "none",
            "--no-color": "none",
            "-o": "none",
            "--only-matching": "none",
            # Context
            "-A": "number",
            "--after-context": "number",
            "-B": "number",
            "--before-context": "number",
            "-C": "number",
            "--context": "number",
            # Boolean operators for multi-pattern
            "--and": "none",
            "--or": "none",
            "--not": "none",
            # Scope control
            "--max-depth": "number",
            "--untracked": "none",
            "--no-index": "none",
            "--recurse-submodules": "none",
            "--cached": "none",
            # Threads
            "--threads": "number",
            # Quiet
            "-q": "none",
            "--quiet": "none",
        },
    },
    # git stash show is read-only
    "git stash show": {
        "safeFlags": {
            **GIT_STAT_FLAGS,
            **GIT_COLOR_FLAGS,
            **GIT_PATCH_FLAGS,
            # Diff options
            "--word-diff": "none",
            "--word-diff-regex": "string",
            "--diff-filter": "string",
            "--abbrev": "number",
        },
    },
    # git worktree list is read-only
    "git worktree list": {
        "safeFlags": {
            "--porcelain": "none",
            "-v": "none",
            "--verbose": "none",
            "--expire": "string",
        },
    },
    "git tag": {
        "safeFlags": {
            # List mode flags
            "-l": "none",
            "--list": "none",
            "-n": "number",
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "string",
            "--no-merged": "string",
            "--sort": "string",
            "--format": "string",
            "--points-at": "string",
            "--column": "none",
            "--no-column": "none",
            "-i": "none",
            "--ignore-case": "none",
        },
        "additionalCommandIsDangerousCallback": _git_tag_is_dangerous,
    },
    "git branch": {
        "safeFlags": {
            # List mode flags
            "-l": "none",
            "--list": "none",
            "-a": "none",
            "--all": "none",
            "-r": "none",
            "--remotes": "none",
            "-v": "none",
            "-vv": "none",
            "--verbose": "none",
            # Display options
            "--color": "none",
            "--no-color": "none",
            "--column": "none",
            "--no-column": "none",
            # SECURITY: --abbrev stays 'number' (attached form safe); detached
            # form blocked by the callback below.
            "--abbrev": "number",
            "--no-abbrev": "none",
            # Filtering - these take commit/ref arguments
            "--contains": "string",
            "--no-contains": "string",
            "--merged": "none",  # Optional commit argument - handled in callback
            "--no-merged": "none",  # Optional commit argument - handled in callback
            "--points-at": "string",
            # Sorting
            "--sort": "string",
            # Note: --format is intentionally excluded
            # Show current
            "--show-current": "none",
            "-i": "none",
            "--ignore-case": "none",
        },
        "additionalCommandIsDangerousCallback": _git_branch_is_dangerous,
    },
}


# ---------------------------------------------------------------------------
# GH_READ_ONLY_COMMANDS — tabvis-only gh CLI commands (network-dependent)
# ---------------------------------------------------------------------------


def gh_is_dangerous_callback(_raw_command: str, args: list[str]) -> bool:
    """Prevent network exfil through gh's ``[HOST/]OWNER/REPO`` argument.

    Rejects any token (or ``--flag=value`` inline value) that is a URL (``://``),
    SSH-style (``@``), or has 2+ slashes (HOST/OWNER/REPO format — normal is the
    single-slash OWNER/REPO). Mirrors git ls-remote's inline URL guard.
    """
    for token in args:
        if not token:
            continue
        # For flag tokens, extract the VALUE after `=` for inspection.
        value = token
        if token.startswith("-"):
            eq_idx = token.find("=")
            if eq_idx == -1:
                continue  # flag without inline value, nothing to inspect
            value = token[eq_idx + 1 :]
            if not value:
                continue
        # Skip values that are clearly not repo specs (no `/`, no `://`, no `@`)
        if "/" not in value and "://" not in value and "@" not in value:
            continue
        # URL schemes: https://, http://, git://, ssh://
        if "://" in value:
            return True
        # SSH-style: git@host:owner/repo
        if "@" in value:
            return True
        # 3+ segments = HOST/OWNER/REPO (normal gh format is OWNER/REPO, 1 slash)
        slash_count = value.count("/")
        if slash_count >= 2:
            return True
    return False


GH_READ_ONLY_COMMANDS: dict[str, ExternalCommandConfig] = {
    # gh pr view is read-only — displays pull request details
    "gh pr view": {
        "safeFlags": {
            "--json": "string",
            "--comments": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh pr list is read-only — lists pull requests
    "gh pr list": {
        "safeFlags": {
            "--state": "string",
            "-s": "string",
            "--author": "string",
            "--assignee": "string",
            "--label": "string",
            "--limit": "number",
            "-L": "number",
            "--base": "string",
            "--head": "string",
            "--search": "string",
            "--json": "string",
            "--draft": "none",
            "--app": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh pr diff is read-only — shows pull request diff
    "gh pr diff": {
        "safeFlags": {
            "--color": "string",
            "--name-only": "none",
            "--patch": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh pr checks is read-only — shows CI status checks
    "gh pr checks": {
        "safeFlags": {
            "--watch": "none",
            "--required": "none",
            "--fail-fast": "none",
            "--json": "string",
            "--interval": "number",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh issue view is read-only — displays issue details
    "gh issue view": {
        "safeFlags": {
            "--json": "string",
            "--comments": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh issue list is read-only — lists issues
    "gh issue list": {
        "safeFlags": {
            "--state": "string",
            "-s": "string",
            "--assignee": "string",
            "--author": "string",
            "--label": "string",
            "--limit": "number",
            "-L": "number",
            "--milestone": "string",
            "--search": "string",
            "--json": "string",
            "--app": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh repo view is read-only — displays repository details
    # NOTE: gh repo view uses a positional argument, not --repo/-R flags
    "gh repo view": {
        "safeFlags": {
            "--json": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh run list is read-only — lists workflow runs
    "gh run list": {
        "safeFlags": {
            "--branch": "string",
            "-b": "string",
            "--status": "string",
            "-s": "string",
            "--workflow": "string",
            "-w": "string",  # NOTE: -w is --workflow here, NOT --web
            "--limit": "number",
            "-L": "number",
            "--json": "string",
            "--repo": "string",
            "-R": "string",
            "--event": "string",
            "-e": "string",
            "--user": "string",
            "-u": "string",
            "--created": "string",
            "--commit": "string",
            "-c": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh run view is read-only — displays a workflow run's details
    "gh run view": {
        "safeFlags": {
            "--log": "none",
            "--log-failed": "none",
            "--exit-status": "none",
            "--verbose": "none",
            "-v": "none",  # NOTE: -v is --verbose here, NOT --web
            "--json": "string",
            "--repo": "string",
            "-R": "string",
            "--job": "string",
            "-j": "string",
            "--attempt": "number",
            "-a": "number",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh auth status is read-only — displays authentication state
    # NOTE: --show-token/-t intentionally excluded (leaks secrets)
    "gh auth status": {
        "safeFlags": {
            "--active": "none",
            "-a": "none",
            "--hostname": "string",
            "-h": "string",
            "--json": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh pr status is read-only — shows your PRs
    "gh pr status": {
        "safeFlags": {
            "--conflict-status": "none",
            "-c": "none",
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh issue status is read-only — shows your issues
    "gh issue status": {
        "safeFlags": {
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh release list is read-only — lists releases
    "gh release list": {
        "safeFlags": {
            "--exclude-drafts": "none",
            "--exclude-pre-releases": "none",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--order": "string",
            "-O": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh release view is read-only — displays release details
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh release view": {
        "safeFlags": {
            "--json": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh workflow list is read-only — lists workflow files
    "gh workflow list": {
        "safeFlags": {
            "--all": "none",
            "-a": "none",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh workflow view is read-only — displays workflow summary
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh workflow view": {
        "safeFlags": {
            "--ref": "string",
            "-r": "string",
            "--yaml": "none",
            "-y": "none",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh label list is read-only — lists labels
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh label list": {
        "safeFlags": {
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--order": "string",
            "--search": "string",
            "-S": "string",
            "--sort": "string",
            "--repo": "string",
            "-R": "string",
        },
        "additionalCommandIsDangerousCallback": gh_is_dangerous_callback,
    },
    # gh search repos is read-only — searches repositories
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh search repos": {
        "safeFlags": {
            "--archived": "none",
            "--created": "string",
            "--followers": "string",
            "--forks": "string",
            "--good-first-issues": "string",
            "--help-wanted-issues": "string",
            "--include-forks": "string",
            "--json": "string",
            "--language": "string",
            "--license": "string",
            "--limit": "number",
            "-L": "number",
            "--match": "string",
            "--number-topics": "string",
            "--order": "string",
            "--owner": "string",
            "--size": "string",
            "--sort": "string",
            "--stars": "string",
            "--topic": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    # gh search issues is read-only — searches issues
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh search issues": {
        "safeFlags": {
            "--app": "string",
            "--assignee": "string",
            "--author": "string",
            "--closed": "string",
            "--commenter": "string",
            "--comments": "string",
            "--created": "string",
            "--include-prs": "none",
            "--interactions": "string",
            "--json": "string",
            "--label": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--locked": "none",
            "--match": "string",
            "--mentions": "string",
            "--milestone": "string",
            "--no-assignee": "none",
            "--no-label": "none",
            "--no-milestone": "none",
            "--no-project": "none",
            "--order": "string",
            "--owner": "string",
            "--project": "string",
            "--reactions": "string",
            "--repo": "string",
            "-R": "string",
            "--sort": "string",
            "--state": "string",
            "--team-mentions": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    # gh search prs is read-only — searches pull requests
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh search prs": {
        "safeFlags": {
            "--app": "string",
            "--assignee": "string",
            "--author": "string",
            "--base": "string",
            "-B": "string",
            "--checks": "string",
            "--closed": "string",
            "--commenter": "string",
            "--comments": "string",
            "--created": "string",
            "--draft": "none",
            "--head": "string",
            "-H": "string",
            "--interactions": "string",
            "--involves": "string",
            "--json": "string",
            "--label": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--locked": "none",
            "--match": "string",
            "--mentions": "string",
            "--merged": "none",
            "--merged-at": "string",
            "--milestone": "string",
            "--no-assignee": "none",
            "--no-label": "none",
            "--no-milestone": "none",
            "--no-project": "none",
            "--order": "string",
            "--owner": "string",
            "--project": "string",
            "--reactions": "string",
            "--repo": "string",
            "-R": "string",
            "--review": "string",
            "--review-requested": "string",
            "--reviewed-by": "string",
            "--sort": "string",
            "--state": "string",
            "--team-mentions": "string",
            "--updated": "string",
            "--visibility": "string",
        },
    },
    # gh search commits is read-only — searches commits
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh search commits": {
        "safeFlags": {
            "--author": "string",
            "--author-date": "string",
            "--author-email": "string",
            "--author-name": "string",
            "--committer": "string",
            "--committer-date": "string",
            "--committer-email": "string",
            "--committer-name": "string",
            "--hash": "string",
            "--json": "string",
            "--limit": "number",
            "-L": "number",
            "--merge": "none",
            "--order": "string",
            "--owner": "string",
            "--parent": "string",
            "--repo": "string",
            "-R": "string",
            "--sort": "string",
            "--tree": "string",
            "--visibility": "string",
        },
    },
    # gh search code is read-only — searches code
    # NOTE: --web/-w intentionally excluded (opens browser)
    "gh search code": {
        "safeFlags": {
            "--extension": "string",
            "--filename": "string",
            "--json": "string",
            "--language": "string",
            "--limit": "number",
            "-L": "number",
            "--match": "string",
            "--owner": "string",
            "--repo": "string",
            "-R": "string",
            "--size": "string",
        },
    },
}


# ---------------------------------------------------------------------------
# DOCKER_READ_ONLY_COMMANDS — docker inspect/logs read-only commands
# ---------------------------------------------------------------------------

DOCKER_READ_ONLY_COMMANDS: dict[str, ExternalCommandConfig] = {
    "docker logs": {
        "safeFlags": {
            "--follow": "none",
            "-f": "none",
            "--tail": "string",
            "-n": "string",
            "--timestamps": "none",
            "-t": "none",
            "--since": "string",
            "--until": "string",
            "--details": "none",
        },
    },
    "docker inspect": {
        "safeFlags": {
            "--format": "string",
            "-f": "string",
            "--type": "string",
            "--size": "none",
            "-s": "none",
        },
    },
}


# ---------------------------------------------------------------------------
# RIPGREP_READ_ONLY_COMMANDS — rg (ripgrep) read-only search
# ---------------------------------------------------------------------------

RIPGREP_READ_ONLY_COMMANDS: dict[str, ExternalCommandConfig] = {
    "rg": {
        "safeFlags": {
            # Pattern flags
            "-e": "string",
            "--regexp": "string",
            "-f": "string",
            # Common search options
            "-i": "none",
            "--ignore-case": "none",
            "-S": "none",
            "--smart-case": "none",
            "-F": "none",
            "--fixed-strings": "none",
            "-w": "none",
            "--word-regexp": "none",
            "-v": "none",
            "--invert-match": "none",
            # Output options
            "-c": "none",
            "--count": "none",
            "-l": "none",
            "--files-with-matches": "none",
            "--files-without-match": "none",
            "-n": "none",
            "--line-number": "none",
            "-o": "none",
            "--only-matching": "none",
            "-A": "number",
            "--after-context": "number",
            "-B": "number",
            "--before-context": "number",
            "-C": "number",
            "--context": "number",
            "-H": "none",
            "-h": "none",
            "--heading": "none",
            "--no-heading": "none",
            "-q": "none",
            "--quiet": "none",
            "--column": "none",
            # File filtering
            "-g": "string",
            "--glob": "string",
            "-t": "string",
            "--type": "string",
            "-T": "string",
            "--type-not": "string",
            "--type-list": "none",
            "--hidden": "none",
            "--no-ignore": "none",
            "-u": "none",
            # Common options
            "-m": "number",
            "--max-count": "number",
            "-d": "number",
            "--max-depth": "number",
            "-a": "none",
            "--text": "none",
            "-z": "none",
            "-L": "none",
            "--follow": "none",
            # Display options
            "--color": "string",
            "--json": "none",
            "--stats": "none",
            # Help and version
            "--help": "none",
            "--version": "none",
            "--debug": "none",
            # Special argument separator
            "--": "none",
        },
    },
}


# ---------------------------------------------------------------------------
# PYRIGHT_READ_ONLY_COMMANDS — pyright static type checker
# ---------------------------------------------------------------------------


def _pyright_is_dangerous(_raw_command: str, args: list[str]) -> bool:
    # Check if --watch or -w appears as a standalone token (flag).
    return any(t == "--watch" or t == "-w" for t in args)


PYRIGHT_READ_ONLY_COMMANDS: dict[str, ExternalCommandConfig] = {
    "pyright": {
        "respectsDoubleDash": False,  # pyright treats -- as a file path, not end-of-options
        "safeFlags": {
            "--outputjson": "none",
            "--project": "string",
            "-p": "string",
            "--pythonversion": "string",
            "--pythonplatform": "string",
            "--typeshedpath": "string",
            "--venvpath": "string",
            "--level": "string",
            "--stats": "none",
            "--verbose": "none",
            "--version": "none",
            "--dependencies": "none",
            "--warnings": "none",
        },
        "additionalCommandIsDangerousCallback": _pyright_is_dangerous,
    },
}


# ---------------------------------------------------------------------------
# EXTERNAL_READONLY_COMMANDS — cross-shell read-only commands
# Only commands that work identically in bash and PowerShell on Windows.
# Unix-specific commands (cat, head, wc, etc.) belong in BashTool's READONLY_COMMANDS.
# ---------------------------------------------------------------------------

EXTERNAL_READONLY_COMMANDS: tuple[str, ...] = (
    "docker ps",
    "docker images",
)


# ---------------------------------------------------------------------------
# UNC path detection (shared across Bash and PowerShell)
# ---------------------------------------------------------------------------

_BACKSLASH_UNC_PATTERN = re.compile(r"\\\\[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE)
# Forward-slash UNC: negative lookbehind (?<!:) to exclude URLs (https://, http://, ftp://).
_FORWARD_SLASH_UNC_PATTERN = re.compile(
    r"(?<!:)//[^\s\\/]+(?:@(?:\d+|ssl))?(?:[\\/]|$|\s)", re.IGNORECASE
)
_MIXED_SLASH_UNC_PATTERN = re.compile(r"/\\{2,}[^\s\\/]")
_REVERSE_MIXED_SLASH_UNC_PATTERN = re.compile(r"\\{2,}/[^\s\\/]")
_WEBDAV_SSL_PORT_PATTERN = re.compile(r"@SSL@\d+", re.IGNORECASE)
_WEBDAV_PORT_SSL_PATTERN = re.compile(r"@\d+@SSL", re.IGNORECASE)
_DAVWWWROOT_PATTERN = re.compile(r"DavWWWRoot", re.IGNORECASE)
_IPV4_BACKSLASH_UNC_PATTERN = re.compile(r"^\\\\(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]")
_IPV4_FORWARD_UNC_PATTERN = re.compile(r"^//(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})[\\/]")
_IPV6_BACKSLASH_UNC_PATTERN = re.compile(r"^\\\\(\[[\da-fA-F:]+\])[\\/]")
_IPV6_FORWARD_UNC_PATTERN = re.compile(r"^//(\[[\da-fA-F:]+\])[\\/]")


def contains_vulnerable_unc_path(path_or_command: str) -> bool:
    """Check if a path/command contains a UNC path that could trigger network requests.

    Detects basic UNC paths, WebDAV patterns, IP-based UNC, and forward-slash
    variants used for NTLM/Kerberos credential leakage or WebDAV attacks.
    Only checks on the Windows platform.
    """
    # Only check on Windows platform
    if get_platform() != "windows":
        return False

    # 1. General UNC paths with backslashes
    if _BACKSLASH_UNC_PATTERN.search(path_or_command):
        return True

    # 2. Forward-slash UNC paths
    if _FORWARD_SLASH_UNC_PATTERN.search(path_or_command):
        return True

    # 3. Mixed-separator UNC paths (forward slash + backslashes)
    if _MIXED_SLASH_UNC_PATTERN.search(path_or_command):
        return True

    # 4. Mixed-separator UNC paths (backslashes + forward slash)
    if _REVERSE_MIXED_SLASH_UNC_PATTERN.search(path_or_command):
        return True

    # 5. WebDAV SSL/port patterns
    if _WEBDAV_SSL_PORT_PATTERN.search(path_or_command) or _WEBDAV_PORT_SSL_PATTERN.search(
        path_or_command
    ):
        return True

    # 6. DavWWWRoot marker (Windows WebDAV redirector)
    if _DAVWWWROOT_PATTERN.search(path_or_command):
        return True

    # 7. UNC paths with IPv4 addresses
    if _IPV4_BACKSLASH_UNC_PATTERN.match(path_or_command) or _IPV4_FORWARD_UNC_PATTERN.match(
        path_or_command
    ):
        return True

    # 8. UNC paths with bracketed IPv6 addresses
    if _IPV6_BACKSLASH_UNC_PATTERN.match(path_or_command) or _IPV6_FORWARD_UNC_PATTERN.match(
        path_or_command
    ):
        return True

    return False


# ---------------------------------------------------------------------------
# Flag validation utilities
# ---------------------------------------------------------------------------

# Regex pattern to match valid flag names (letters, digits, underscores, hyphens)
FLAG_PATTERN = re.compile(r"^-[a-zA-Z0-9_-]")

_NUMBER_RE = re.compile(r"^\d+$")
_GIT_NUMERIC_SHORTHAND_RE = re.compile(r"^-\d+$")
_GIT_SORT_REVERSE_RE = re.compile(r"^-[a-zA-Z]")


def validate_flag_argument(value: str, arg_type: FlagArgType) -> bool:
    """Validates flag arguments based on their expected type."""
    if arg_type == "none":
        return False  # Should not have been called for 'none' type
    if arg_type == "number":
        return bool(_NUMBER_RE.match(value))
    if arg_type == "string":
        return True  # Any string including empty is valid
    if arg_type == "char":
        return len(value) == 1
    if arg_type == "{}":
        return value == "{}"
    if arg_type == "EOF":
        return value == "EOF"
    return False


class ValidateFlagsOptions(TypedDict, total=False):
    """Options for :func:`validate_flags`.

    - ``commandName``: for command-specific handling (git numeric shorthand,
      grep/rg attached numeric).
    - ``rawCommand``: passed to ``additionalCommandIsDangerousCallback``.
    - ``xargsTargetCommands``: if provided, enables xargs-style target detection.
    """

    commandName: str
    rawCommand: str
    xargsTargetCommands: list[str]


def validate_flags(
    tokens: list[str],
    start_index: int,
    config: ExternalCommandConfig,
    options: ValidateFlagsOptions | None = None,
) -> bool:
    """Validates the flags/arguments portion of a tokenized command against a config.

    This is the flag-walking loop extracted from BashTool's
    ``isCommandSafeViaFlagParsing``.

    Args:
        tokens: Pre-tokenized args (from bash shell-quote or PowerShell AST).
        start_index: Where to start validating (after command tokens).
        config: The safe flags config.
        options: Optional command-specific handling, raw command, and xargs targets.

    Returns:
        True if all flags are valid, False otherwise.
    """
    opts: ValidateFlagsOptions = options or {}
    safe_flags = config["safeFlags"]
    command_name = opts.get("commandName")
    xargs_target_commands = opts.get("xargsTargetCommands")
    respects_double_dash = config.get("respectsDoubleDash", True)

    i = start_index

    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue

        # Special handling for xargs: once we find the target command, stop validating flags.
        if (
            xargs_target_commands is not None
            and command_name == "xargs"
            and (not token.startswith("-") or token == "--")
        ):
            if token == "--" and i + 1 < len(tokens):
                i += 1
                token = tokens[i]
            if token and token in xargs_target_commands:
                break
            return False

        if token == "--":
            # SECURITY: Only break if the tool respects POSIX `--` (default: True).
            if respects_double_dash is not False:
                i += 1
                break  # Everything after -- is arguments
            # Tool doesn't respect --: treat as positional arg, keep validating.
            i += 1
            continue

        if token.startswith("-") and len(token) > 1 and FLAG_PATTERN.match(token):
            # Handle --flag=value format.
            # SECURITY: Track whether the token CONTAINS `=` separately from whether
            # the value is non-empty. `-E=` has has_equals=True but inline_value=''.
            has_equals = "=" in token
            flag, *value_parts = token.split("=")
            inline_value = "=".join(value_parts)

            if not flag:
                return False

            flag_arg_type = safe_flags.get(flag)

            if not flag_arg_type:
                # Special case: git commands support -<number> as shorthand for -n <number>.
                if command_name == "git" and _GIT_NUMERIC_SHORTHAND_RE.match(flag):
                    i += 1
                    continue

                # Handle flags with directly attached numeric arguments (e.g., -A20, -B10).
                # Only apply this special handling to grep and rg commands.
                if (
                    command_name in ("grep", "rg")
                    and flag.startswith("-")
                    and not flag.startswith("--")
                    and len(flag) > 2
                ):
                    potential_flag = flag[0:2]  # e.g., '-A' from '-A20'
                    potential_value = flag[2:]  # e.g., '20' from '-A20'

                    if safe_flags.get(potential_flag) and _NUMBER_RE.match(potential_value):
                        inner_arg_type = safe_flags[potential_flag]
                        if inner_arg_type in ("number", "string"):
                            if validate_flag_argument(potential_value, inner_arg_type):
                                i += 1
                                continue
                            else:
                                return False  # Invalid attached value

                # Handle combined single-letter flags like -nr.
                # SECURITY: ALL bundled flags must have arg type 'none'; an
                # arg-taking flag in a bundle consumes the NEXT token in GNU getopt.
                if flag.startswith("-") and not flag.startswith("--") and len(flag) > 2:
                    for j in range(1, len(flag)):
                        single_flag = "-" + flag[j]
                        flag_type = safe_flags.get(single_flag)
                        if not flag_type:
                            return False  # One of the combined flags is not safe
                        if flag_type != "none":
                            return False  # Arg-taking flag in a bundle — cannot safely validate
                    i += 1
                    continue
                else:
                    return False  # Unknown flag

            # Validate flag arguments.
            if flag_arg_type == "none":
                # SECURITY: has_equals covers `-FLAG=` (empty inline).
                if has_equals:
                    return False  # Flag should not have a value
                i += 1
            else:
                # SECURITY: Use has_equals (not inline_value truthiness).
                if has_equals:
                    arg_value = inline_value
                    i += 1
                else:
                    # Check if next token is the argument.
                    next_token = tokens[i + 1] if i + 1 < len(tokens) else None
                    if i + 1 >= len(tokens) or (
                        next_token
                        and next_token.startswith("-")
                        and len(next_token) > 1
                        and FLAG_PATTERN.match(next_token)
                    ):
                        return False  # Missing required argument
                    arg_value = tokens[i + 1] or ""
                    i += 2

                # Defense-in-depth: For string arguments, reject values starting with '-'.
                # Exception: git's --sort flag can have values starting with '-' for reverse sort.
                if flag_arg_type == "string" and arg_value.startswith("-"):
                    if (
                        flag == "--sort"
                        and command_name == "git"
                        and _GIT_SORT_REVERSE_RE.match(arg_value)
                    ):
                        # Looks like a reverse sort (e.g., -refname) — allow.
                        pass
                    else:
                        return False

                # Validate argument based on type.
                if not validate_flag_argument(arg_value, flag_arg_type):
                    return False
        else:
            # Non-flag argument (like revision specs, file paths, etc.) - this is allowed.
            i += 1

    return True
