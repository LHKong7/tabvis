"""BashTool read-only classification.

``check_read_only_constraints`` decides whether a bash command (possibly compound) is purely
read-only and therefore auto-allowable. It combines:

  * a declarative ``COMMAND_ALLOWLIST`` of safe flags per command (``isCommandSafeViaFlagParsing``),
  * a set of ``READONLY_COMMAND_REGEXES`` for simpler commands,
  * unquoted-glob / shell-expansion detection,
  * git sandbox-escape guards (bare-repo, cd+git, git-internal-path writes), and
  * the shared ``bash_security`` safety pre-check.

Cycle note: ``bash_permissions`` (``is_normalized_git_command``) is a cyclic sibling, imported
**lazily** (function-local). ``tabvis.agent.tools.bash_tool`` is referenced only for the input type
(``Any`` at runtime, ``BashToolInput`` under ``TYPE_CHECKING``); never top-level imported.
``path_validation`` and ``bash_security`` are siblings that import standalone (no back-edge to
this module), so they are imported normally.

Casing: Python identifiers are snake_case; ``PermissionResult`` dicts keep their camelCase wire
keys (``behavior``, ``message``, ``updatedInput``); ``CommandConfig`` keeps camelCase wire keys
(``safeFlags``/``respectsDoubleDash``/``additionalCommandIsDangerousCallback``/``regex``) so it
round-trips into ``validate_flags``.
"""

from __future__ import annotations

import os
import os.path
import re
from typing import TYPE_CHECKING, Any

from tabvis.bootstrap.state import get_original_cwd
from tabvis.agent.tools.bash_security import bash_command_is_safe_deprecated
from tabvis.agent.tools.path_validation import COMMAND_OPERATION_TYPE, PATH_EXTRACTORS
from tabvis.agent.tools.sed_validation import sed_command_is_allowed_by_allowlist
from tabvis.utils.bash.commands import (
    extract_output_redirections,
    split_command_deprecated,
)
from tabvis.utils.bash.shell_quote import try_parse_shell_command
from tabvis.utils.cwd import get_cwd
from tabvis.utils.platform import get_platform
from tabvis.utils.sandbox.sandbox_adapter import SandboxManager
from tabvis.utils.shell.read_only_command_validation import (
    DOCKER_READ_ONLY_COMMANDS,
    EXTERNAL_READONLY_COMMANDS,
    GH_READ_ONLY_COMMANDS,
    GIT_READ_ONLY_COMMANDS,
    PYRIGHT_READ_ONLY_COMMANDS,
    RIPGREP_READ_ONLY_COMMANDS,
    contains_vulnerable_unc_path,
    validate_flags,
)

if TYPE_CHECKING:
    from tabvis.agent.tools.bash_tool import BashToolInput  # noqa: F401 — type-only
    from tabvis.types.permissions import PermissionResult  # noqa: F401 — type-only

# Runtime alias: PermissionResult is a plain dict (TypedDict union keyed by ``behavior``).
PermissionResult = dict  # noqa: F811 — runtime alias for the TYPE_CHECKING import


# ───────────────────────────────────────────────────────────────────────────
# CommandConfig is a plain dict shape consumed by validate_flags: {safeFlags,
# regex?, respectsDoubleDash?, additionalCommandIsDangerousCallback?}.
# ───────────────────────────────────────────────────────────────────────────

# Shared safe flags for fd and fdfind. -x/--exec and -X/--exec-batch deliberately excluded.
FD_SAFE_FLAGS: dict[str, str] = {
    "-h": "none", "--help": "none", "-V": "none", "--version": "none",
    "-H": "none", "--hidden": "none", "-I": "none", "--no-ignore": "none",
    "--no-ignore-vcs": "none", "--no-ignore-parent": "none", "-s": "none",
    "--case-sensitive": "none", "-i": "none", "--ignore-case": "none",
    "-g": "none", "--glob": "none", "--regex": "none", "-F": "none",
    "--fixed-strings": "none", "-a": "none", "--absolute-path": "none",
    "-L": "none", "--follow": "none", "-p": "none", "--full-path": "none",
    "-0": "none", "--print0": "none", "-d": "number", "--max-depth": "number",
    "--min-depth": "number", "--exact-depth": "number", "-t": "string",
    "--type": "string", "-e": "string", "--extension": "string", "-S": "string",
    "--size": "string", "--changed-within": "string", "--changed-before": "string",
    "-o": "string", "--owner": "string", "-E": "string", "--exclude": "string",
    "--ignore-file": "string", "-c": "string", "--color": "string", "-j": "number",
    "--threads": "number", "--max-buffer-time": "string", "--max-results": "number",
    "-1": "none", "-q": "none", "--quiet": "none", "--show-errors": "none",
    "--strip-cwd-prefix": "none", "--one-file-system": "none", "--prune": "none",
    "--search-path": "string", "--base-directory": "string", "--path-separator": "string",
    "--batch-size": "number", "--no-require-git": "none", "--hyperlink": "string",
    "--and": "string", "--format": "string",
}


def _ps_callback(_raw_command: str, args: list[str]) -> bool:
    # Block BSD-style 'e' modifier (letter-only token containing 'e') — shows env vars.
    return any(not a.startswith("-") and re.fullmatch(r"[a-zA-Z]*e[a-zA-Z]*", a) for a in args)


_DATE_FLAGS_WITH_ARGS = {"-d", "--date", "-r", "--reference", "--iso-8601", "--rfc-3339"}


def _date_callback(_raw_command: str, args: list[str]) -> bool:
    # Positional args must start with + (format strings); else they could set system time.
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--") and "=" in token:
            i += 1
        elif token.startswith("-"):
            if token in _DATE_FLAGS_WITH_ARGS:
                i += 2
            else:
                i += 1
        else:
            if not token.startswith("+"):
                return True  # dangerous
            i += 1
    return False


def _lsof_callback(_raw_command: str, args: list[str]) -> bool:
    # Block +m (create mount supplement file) — writes to disk.
    return any(a == "+m" or a.startswith("+m") for a in args)


_TPUT_DANGEROUS_CAPABILITIES = {
    "init", "reset", "rs1", "rs2", "rs3", "is1", "is2", "is3", "iprog", "if", "rf",
    "clear", "flash", "mc0", "mc4", "mc5", "mc5i", "mc5p", "pfkey", "pfloc", "pfx",
    "pfxl", "smcup", "rmcup",
}


def _tput_callback(_raw_command: str, args: list[str]) -> bool:
    flags_with_args = {"-T"}
    i = 0
    after_double_dash = False
    while i < len(args):
        token = args[i]
        if token == "--":
            after_double_dash = True
            i += 1
        elif not after_double_dash and token.startswith("-"):
            if token == "-S":
                return True
            if not token.startswith("--") and len(token) > 2 and "S" in token:
                return True
            if token in flags_with_args:
                i += 2
            else:
                i += 1
        else:
            if token in _TPUT_DANGEROUS_CAPABILITIES:
                return True
            i += 1
    return False


def _sed_callback(raw_command: str, _args: list[str]) -> bool:
    return not sed_command_is_allowed_by_allowlist(raw_command)


# Central allowlist for safe (read-only) commands. Keys may be multi-word ("git diff").
COMMAND_ALLOWLIST: dict[str, dict] = {
    "xargs": {
        "safeFlags": {
            "-I": "{}", "-n": "number", "-P": "number", "-L": "number",
            "-s": "number", "-E": "EOF", "-0": "none", "-t": "none", "-r": "none",
            "-x": "none", "-d": "char",
        },
    },
    **GIT_READ_ONLY_COMMANDS,
    "file": {
        "safeFlags": {
            "--brief": "none", "-b": "none", "--mime": "none", "-i": "none",
            "--mime-type": "none", "--mime-encoding": "none", "--apple": "none",
            "--check-encoding": "none", "-c": "none", "--exclude": "string",
            "--exclude-quiet": "string", "--print0": "none", "-0": "none",
            "-f": "string", "-F": "string", "--separator": "string",
            "--help": "none", "--version": "none", "-v": "none",
            "--no-dereference": "none", "-h": "none", "--dereference": "none",
            "-L": "none", "--magic-file": "string", "-m": "string",
            "--keep-going": "none", "-k": "none", "--list": "none", "-l": "none",
            "--no-buffer": "none", "-n": "none", "--preserve-date": "none",
            "-p": "none", "--raw": "none", "-r": "none", "-s": "none",
            "--special-files": "none", "--uncompress": "none", "-z": "none",
        },
    },
    "sed": {
        "safeFlags": {
            "--expression": "string", "-e": "string", "--quiet": "none",
            "--silent": "none", "-n": "none", "--regexp-extended": "none",
            "-r": "none", "--posix": "none", "-E": "none", "--line-length": "number",
            "-l": "number", "--zero-terminated": "none", "-z": "none",
            "--separate": "none", "-s": "none", "--unbuffered": "none", "-u": "none",
            "--debug": "none", "--help": "none", "--version": "none",
        },
        "additionalCommandIsDangerousCallback": _sed_callback,
    },
    "sort": {
        "safeFlags": {
            "--ignore-leading-blanks": "none", "-b": "none", "--dictionary-order": "none",
            "-d": "none", "--ignore-case": "none", "-f": "none",
            "--general-numeric-sort": "none", "-g": "none", "--human-numeric-sort": "none",
            "-h": "none", "--ignore-nonprinting": "none", "-i": "none", "--month-sort": "none",
            "-M": "none", "--numeric-sort": "none", "-n": "none", "--random-sort": "none",
            "-R": "none", "--reverse": "none", "-r": "none", "--sort": "string",
            "--stable": "none", "-s": "none", "--unique": "none", "-u": "none",
            "--version-sort": "none", "-V": "none", "--zero-terminated": "none", "-z": "none",
            "--key": "string", "-k": "string", "--field-separator": "string", "-t": "string",
            "--check": "none", "-c": "none", "--check-char-order": "none", "-C": "none",
            "--merge": "none", "-m": "none", "--buffer-size": "string", "-S": "string",
            "--parallel": "number", "--batch-size": "number", "--help": "none",
            "--version": "none",
        },
    },
    "man": {
        "safeFlags": {
            "-a": "none", "--all": "none", "-d": "none", "-f": "none", "--whatis": "none",
            "-h": "none", "-k": "none", "--apropos": "none", "-l": "string", "-w": "none",
            "-S": "string", "-s": "string",
        },
    },
    "help": {
        "safeFlags": {"-d": "none", "-m": "none", "-s": "none"},
    },
    "netstat": {
        "safeFlags": {
            "-a": "none", "-L": "none", "-l": "none", "-n": "none", "-f": "string",
            "-g": "none", "-i": "none", "-I": "string", "-s": "none", "-r": "none",
            "-m": "none", "-v": "none",
        },
    },
    "ps": {
        "safeFlags": {
            "-e": "none", "-A": "none", "-a": "none", "-d": "none", "-N": "none",
            "--deselect": "none", "-f": "none", "-F": "none", "-l": "none", "-j": "none",
            "-y": "none", "-w": "none", "-ww": "none", "--width": "number", "-c": "none",
            "-H": "none", "--forest": "none", "--headers": "none", "--no-headers": "none",
            "-n": "string", "--sort": "string", "-L": "none", "-T": "none", "-m": "none",
            "-C": "string", "-G": "string", "-g": "string", "-p": "string", "--pid": "string",
            "-q": "string", "--quick-pid": "string", "-s": "string", "--sid": "string",
            "-t": "string", "--tty": "string", "-U": "string", "-u": "string",
            "--user": "string", "--help": "none", "--info": "none", "-V": "none",
            "--version": "none",
        },
        "additionalCommandIsDangerousCallback": _ps_callback,
    },
    "base64": {
        "respectsDoubleDash": False,
        "safeFlags": {
            "-d": "none", "-D": "none", "--decode": "none", "-b": "number",
            "--break": "number", "-w": "number", "--wrap": "number", "-i": "string",
            "--input": "string", "--ignore-garbage": "none", "-h": "none",
            "--help": "none", "--version": "none",
        },
    },
    "grep": {
        "safeFlags": {
            "-e": "string", "--regexp": "string", "-f": "string", "--file": "string",
            "-F": "none", "--fixed-strings": "none", "-G": "none", "--basic-regexp": "none",
            "-E": "none", "--extended-regexp": "none", "-P": "none", "--perl-regexp": "none",
            "-i": "none", "--ignore-case": "none", "--no-ignore-case": "none", "-v": "none",
            "--invert-match": "none", "-w": "none", "--word-regexp": "none", "-x": "none",
            "--line-regexp": "none", "-c": "none", "--count": "none", "--color": "string",
            "--colour": "string", "-L": "none", "--files-without-match": "none", "-l": "none",
            "--files-with-matches": "none", "-m": "number", "--max-count": "number",
            "-o": "none", "--only-matching": "none", "-q": "none", "--quiet": "none",
            "--silent": "none", "-s": "none", "--no-messages": "none", "-b": "none",
            "--byte-offset": "none", "-H": "none", "--with-filename": "none", "-h": "none",
            "--no-filename": "none", "--label": "string", "-n": "none", "--line-number": "none",
            "-T": "none", "--initial-tab": "none", "-u": "none", "--unix-byte-offsets": "none",
            "-Z": "none", "--null": "none", "-z": "none", "--null-data": "none",
            "-A": "number", "--after-context": "number", "-B": "number",
            "--before-context": "number", "-C": "number", "--context": "number",
            "--group-separator": "string", "--no-group-separator": "none", "-a": "none",
            "--text": "none", "--binary-files": "string", "-D": "string", "--devices": "string",
            "-d": "string", "--directories": "string", "--exclude": "string",
            "--exclude-from": "string", "--exclude-dir": "string", "--include": "string",
            "-r": "none", "--recursive": "none", "-R": "none", "--dereference-recursive": "none",
            "--line-buffered": "none", "-U": "none", "--binary": "none", "--help": "none",
            "-V": "none", "--version": "none",
        },
    },
    **RIPGREP_READ_ONLY_COMMANDS,
    "sha256sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    },
    "sha1sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    },
    "md5sum": {
        "safeFlags": {
            "-b": "none", "--binary": "none", "-t": "none", "--text": "none", "-c": "none",
            "--check": "none", "--ignore-missing": "none", "--quiet": "none", "--status": "none",
            "--strict": "none", "-w": "none", "--warn": "none", "--tag": "none", "-z": "none",
            "--zero": "none", "--help": "none", "--version": "none",
        },
    },
    "tree": {
        "safeFlags": {
            "-a": "none", "-d": "none", "-l": "none", "-f": "none", "-x": "none",
            "-L": "number", "-P": "string", "-I": "string", "--gitignore": "none",
            "--gitfile": "string", "--ignore-case": "none", "--matchdirs": "none",
            "--metafirst": "none", "--prune": "none", "--info": "none", "--infofile": "string",
            "--noreport": "none", "--charset": "string", "--filelimit": "number", "-q": "none",
            "-N": "none", "-Q": "none", "-p": "none", "-u": "none", "-g": "none", "-s": "none",
            "-h": "none", "--si": "none", "--du": "none", "-D": "none", "--timefmt": "string",
            "-F": "none", "--inodes": "none", "--device": "none", "-v": "none", "-t": "none",
            "-c": "none", "-U": "none", "-r": "none", "--dirsfirst": "none",
            "--filesfirst": "none", "--sort": "string", "-i": "none", "-A": "none", "-S": "none",
            "-n": "none", "-C": "none", "-X": "none", "-J": "none", "-H": "string",
            "--nolinks": "none", "--hintro": "string", "--houtro": "string", "-T": "string",
            "--hyperlink": "none", "--scheme": "string", "--authority": "string",
            "--fromfile": "none", "--fromtabfile": "none", "--fflinks": "none", "--help": "none",
            "--version": "none",
        },
    },
    "date": {
        "safeFlags": {
            "-d": "string", "--date": "string", "-r": "string", "--reference": "string",
            "-u": "none", "--utc": "none", "--universal": "none", "-I": "none",
            "--iso-8601": "string", "-R": "none", "--rfc-email": "none", "--rfc-3339": "string",
            "--debug": "none", "--help": "none", "--version": "none",
        },
        "additionalCommandIsDangerousCallback": _date_callback,
    },
    "hostname": {
        "safeFlags": {
            "-f": "none", "--fqdn": "none", "--long": "none", "-s": "none", "--short": "none",
            "-i": "none", "--ip-address": "none", "-I": "none", "--all-ip-addresses": "none",
            "-a": "none", "--alias": "none", "-d": "none", "--domain": "none", "-A": "none",
            "--all-fqdns": "none", "-v": "none", "--verbose": "none", "-h": "none",
            "--help": "none", "-V": "none", "--version": "none",
        },
        "regex": re.compile(r"^hostname(?:\s+(?:-[a-zA-Z]|--[a-zA-Z-]+))*\s*$"),
    },
    "info": {
        "safeFlags": {
            "-f": "string", "--file": "string", "-d": "string", "--directory": "string",
            "-n": "string", "--node": "string", "-a": "none", "--all": "none", "-k": "string",
            "--apropos": "string", "-w": "none", "--where": "none", "--location": "none",
            "--show-options": "none", "--vi-keys": "none", "--subnodes": "none", "-h": "none",
            "--help": "none", "--usage": "none", "--version": "none",
        },
    },
    "lsof": {
        "safeFlags": {
            "-?": "none", "-h": "none", "-v": "none", "-a": "none", "-b": "none", "-C": "none",
            "-l": "none", "-n": "none", "-N": "none", "-O": "none", "-P": "none", "-Q": "none",
            "-R": "none", "-t": "none", "-U": "none", "-V": "none", "-X": "none", "-H": "none",
            "-E": "none", "-F": "none", "-g": "none", "-i": "none", "-K": "none", "-L": "none",
            "-o": "none", "-r": "none", "-s": "none", "-S": "none", "-T": "none", "-x": "none",
            "-A": "string", "-c": "string", "-d": "string", "-e": "string", "-k": "string",
            "-p": "string", "-u": "string",
        },
        "additionalCommandIsDangerousCallback": _lsof_callback,
    },
    "pgrep": {
        "safeFlags": {
            "-d": "string", "--delimiter": "string", "-l": "none", "--list-name": "none",
            "-a": "none", "--list-full": "none", "-v": "none", "--inverse": "none", "-w": "none",
            "--lightweight": "none", "-c": "none", "--count": "none", "-f": "none",
            "--full": "none", "-g": "string", "--pgroup": "string", "-G": "string",
            "--group": "string", "-i": "none", "--ignore-case": "none", "-n": "none",
            "--newest": "none", "-o": "none", "--oldest": "none", "-O": "string",
            "--older": "string", "-P": "string", "--parent": "string", "-s": "string",
            "--session": "string", "-t": "string", "--terminal": "string", "-u": "string",
            "--euid": "string", "-U": "string", "--uid": "string", "-x": "none",
            "--exact": "none", "-F": "string", "--pidfile": "string", "-L": "none",
            "--logpidfile": "none", "-r": "string", "--runstates": "string", "--ns": "string",
            "--nslist": "string", "--help": "none", "-V": "none", "--version": "none",
        },
    },
    "tput": {
        "safeFlags": {"-T": "string", "-V": "none", "-x": "none"},
        "additionalCommandIsDangerousCallback": _tput_callback,
    },
    "ss": {
        "safeFlags": {
            "-h": "none", "--help": "none", "-V": "none", "--version": "none", "-n": "none",
            "--numeric": "none", "-r": "none", "--resolve": "none", "-a": "none", "--all": "none",
            "-l": "none", "--listening": "none", "-o": "none", "--options": "none", "-e": "none",
            "--extended": "none", "-m": "none", "--memory": "none", "-p": "none",
            "--processes": "none", "-i": "none", "--info": "none", "-s": "none",
            "--summary": "none", "-4": "none", "--ipv4": "none", "-6": "none", "--ipv6": "none",
            "-0": "none", "--packet": "none", "-t": "none", "--tcp": "none", "-M": "none",
            "--mptcp": "none", "-S": "none", "--sctp": "none", "-u": "none", "--udp": "none",
            "-d": "none", "--dccp": "none", "-w": "none", "--raw": "none", "-x": "none",
            "--unix": "none", "--tipc": "none", "--vsock": "none", "-f": "string",
            "--family": "string", "-A": "string", "--query": "string", "--socket": "string",
            "-Z": "none", "--context": "none", "-z": "none", "--contexts": "none", "-b": "none",
            "--bpf": "none", "-E": "none", "--events": "none", "-H": "none", "--no-header": "none",
            "-O": "none", "--oneline": "none", "--tipcinfo": "none", "--tos": "none",
            "--cgroup": "none", "--inet-sockopt": "none",
        },
    },
    "fd": {"safeFlags": dict(FD_SAFE_FLAGS)},
    "fdfind": {"safeFlags": dict(FD_SAFE_FLAGS)},
    **PYRIGHT_READ_ONLY_COMMANDS,
    **DOCKER_READ_ONLY_COMMANDS,
}

# gh commands are tabvis-only (network requests). Gated behind USER_TYPE=ant.
ANT_ONLY_COMMAND_ALLOWLIST: dict[str, dict] = {
    **GH_READ_ONLY_COMMANDS,
    "aki": {
        "safeFlags": {
            "-h": "none", "--help": "none", "-k": "none", "--keyword": "none", "-s": "none",
            "--semantic": "none", "--no-adaptive": "none", "-n": "number", "--limit": "number",
            "-o": "number", "--offset": "number", "--source": "string",
            "--exclude-source": "string", "-a": "string", "--after": "string", "-b": "string",
            "--before": "string", "--collection": "string", "--drive": "string",
            "--folder": "string", "--descendants": "none", "-m": "string", "--meta": "string",
            "-t": "string", "--threshold": "string", "--kw-weight": "string",
            "--sem-weight": "string", "-j": "none", "--json": "none", "-c": "none",
            "--chunk": "none", "--preview": "none", "-d": "none", "--full-doc": "none",
            "-v": "none", "--verbose": "none", "--stats": "none", "-S": "number",
            "--summarize": "number", "--explain": "none", "--examine": "string",
            "--url": "string", "--multi-turn": "number", "--multi-turn-model": "string",
            "--multi-turn-context": "string", "--no-rerank": "none", "--audit": "none",
            "--local": "none", "--staging": "none",
        },
    },
}


def _get_command_allowlist() -> dict[str, dict]:
    allowlist = COMMAND_ALLOWLIST
    # On Windows, xargs can be a data-to-code bridge for UNC SMB resolution.
    if get_platform() == "windows":
        allowlist = {k: v for k, v in allowlist.items() if k != "xargs"}
    return allowlist


# Commands safe as xargs targets for auto-approval (purely read-only, no dangerous flags).
SAFE_TARGET_COMMANDS_FOR_XARGS = ["echo", "printf", "wc", "grep", "head", "tail"]

_BACKTICK_RE = re.compile(r"`")
_NEWLINE_RE = re.compile(r"[\n\r]")


def is_command_safe_via_flag_parsing(command: str) -> bool:
    """Validate a single command against ``COMMAND_ALLOWLIST`` flag-by-flag.

    Returns True only when every token is recognized as a safe flag/value.
    """
    parse_result = try_parse_shell_command(command, lambda env: f"${env}")
    if not parse_result["success"]:
        return False

    parsed: list = []
    for token in parse_result["tokens"]:
        if not isinstance(token, str):
            if isinstance(token, dict) and token.get("op") == "glob":
                parsed.append(token["pattern"])
                continue
        parsed.append(token)

    # If there are operators (pipes, redirects), it's not a simple command.
    has_operators = any(not isinstance(token, str) for token in parsed)
    if has_operators:
        return False

    tokens: list[str] = parsed

    if len(tokens) == 0:
        return False

    command_config: dict | None = None
    command_tokens = 0

    allowlist = _get_command_allowlist()
    for cmd_pattern in allowlist:
        cmd_tokens = cmd_pattern.split(" ")
        if len(tokens) >= len(cmd_tokens):
            matches = True
            for i in range(len(cmd_tokens)):
                if tokens[i] != cmd_tokens[i]:
                    matches = False
                    break
            if matches:
                command_config = allowlist[cmd_pattern]
                command_tokens = len(cmd_tokens)
                break

    if not command_config:
        return False

    # git ls-remote: reject URLs / variable refs (data exfiltration).
    if len(tokens) > 1 and tokens[0] == "git" and tokens[1] == "ls-remote":
        for i in range(2, len(tokens)):
            token = tokens[i]
            if token and not token.startswith("-"):
                if "://" in token:
                    return False
                if "@" in token or ":" in token:
                    return False
                if "$" in token:
                    return False

    # Reject ANY token containing `$` (variable expansion) or brace expansion.
    for i in range(command_tokens, len(tokens)):
        token = tokens[i]
        if not token:
            continue
        if "$" in token:
            return False
        if "{" in token and ("," in token or ".." in token):
            return False

    if not validate_flags(
        tokens,
        command_tokens,
        command_config,
        {
            "commandName": tokens[0],
            "rawCommand": command,
            "xargsTargetCommands": (
                SAFE_TARGET_COMMANDS_FOR_XARGS if tokens[0] == "xargs" else None
            ),
        },
    ):
        return False

    config_regex = command_config.get("regex")
    if config_regex and not config_regex.search(command):
        return False
    if not config_regex and _BACKTICK_RE.search(command):
        return False
    # Block newlines/CR in grep/rg patterns (injection).
    if (
        not config_regex
        and tokens[0] in ("rg", "grep")
        and _NEWLINE_RE.search(command)
    ):
        return False
    callback = command_config.get("additionalCommandIsDangerousCallback")
    if callback and callback(command, tokens[command_tokens:]):
        return False

    return True


def _make_regex_for_safe_command(command: str) -> re.Pattern[str]:
    """Build a regex matching safe invocations of ``command`` (blocks shell metacharacters)."""
    return re.compile(rf"^{command}(?:\s|$)[^<>()$`|{{}}&;\n\r]*$")


# Simple read-only commands → regex patterns.
READONLY_COMMANDS = [
    *EXTERNAL_READONLY_COMMANDS,
    "cal", "uptime",
    "cat", "head", "tail", "wc", "stat", "strings", "hexdump", "od", "nl",
    "id", "uname", "free", "df", "du", "locale", "groups", "nproc",
    "basename", "dirname", "realpath",
    "cut", "paste", "tr", "column", "tac", "rev", "fold", "expand", "unexpand",
    "fmt", "comm", "cmp", "numfmt",
    "readlink",
    "diff",
    "true", "false",
    "sleep", "which", "type", "expr", "test", "getconf",
    "seq", "tsort", "pr",
]

# Complex commands that require custom regex patterns.
READONLY_COMMAND_REGEXES: list[re.Pattern[str]] = [
    *[_make_regex_for_safe_command(c) for c in READONLY_COMMANDS],
    re.compile(r"""^echo(?:\s+(?:'[^']*'|"[^"$<>\n\r]*"|[^|;&`$(){}><#\\!"'\s]+))*(?:\s+2>&1)?\s*$"""),
    re.compile(r"^tabvis -h$"),
    re.compile(r"^tabvis --help$"),
    re.compile(r"^uniq(?:\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\S+)?|-[fsw]\s+\d+))*(?:\s|$)\s*$"),
    re.compile(r"^pwd$"),
    re.compile(r"^whoami$"),
    re.compile(r"^node -v$"),
    re.compile(r"^node --version$"),
    re.compile(r"^python --version$"),
    re.compile(r"^python3 --version$"),
    re.compile(r"^history(?:\s+\d+)?\s*$"),
    re.compile(r"^alias$"),
    re.compile(r"^arch(?:\s+(?:--help|-h))?\s*$"),
    re.compile(r"^ip addr$"),
    re.compile(r"^ifconfig(?:\s+[a-zA-Z][a-zA-Z0-9_-]*)?\s*$"),
    re.compile(
        r"^jq(?!\s+.*(?:-f\b|--from-file|--rawfile|--slurpfile|--run-tests|-L\b|"
        r"--library-path|\benv\b|\$ENV\b))"
        r"(?:\s+(?:-[a-zA-Z]+|--[a-zA-Z-]+(?:=\S+)?))*"
        r"""(?:\s+'[^'`]*'|\s+"[^"`]*"|\s+[^-\s'"][^\s]*)+\s*$"""
    ),
    re.compile(r"^cd(?:\s+(?:'[^']*'|\"[^\"]*\"|[^\s;|&`$(){}><#\\]+))?$"),
    re.compile(r"^ls(?:\s+[^<>()$`|{}&;\n\r]*)?$"),
    re.compile(
        r"^find(?:\s+(?:\\[()]|(?!-delete\b|-exec\b|-execdir\b|-ok\b|-okdir\b|"
        r"-fprint0?\b|-fls\b|-fprintf\b)[^<>()$`|{}&;\n\r\s]|\s)+)?$"
    ),
]


_DOLLAR_NEXT_RE = re.compile(r"[A-Za-z_@*#?!$0-9-]")
_GLOB_CHAR_RE = re.compile(r"[?*[\]]")


def contains_unquoted_expansion(command: str) -> bool:
    """True if ``command`` contains an unquoted glob or expandable ``$`` (could bypass regexes)."""
    in_single_quote = False
    in_double_quote = False
    escaped = False

    for i, current_char in enumerate(command):
        if escaped:
            escaped = False
            continue

        # Only treat backslash as escape OUTSIDE single quotes.
        if current_char == "\\" and not in_single_quote:
            escaped = True
            continue

        if current_char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if current_char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if in_single_quote:
            continue

        # $ expands inside double quotes AND unquoted.
        if current_char == "$":
            nxt = command[i + 1] if i + 1 < len(command) else None
            if nxt and _DOLLAR_NEXT_RE.match(nxt):
                return True

        # Globs are literal inside double quotes too.
        if in_double_quote:
            continue

        if current_char and _GLOB_CHAR_RE.match(current_char):
            return True

    return False


_GIT_C_RE = re.compile(r"\s-c[\s=]")
_GIT_EXEC_PATH_RE = re.compile(r"\s--exec-path[\s=]")
_GIT_CONFIG_ENV_RE = re.compile(r"\s--config-env[\s=]")


def is_command_read_only(command: str) -> bool:
    """True if a single command string is read-only per the allowlist/regex tables."""
    test_command = command.strip()
    if test_command.endswith(" 2>&1"):
        test_command = test_command[:-5].strip()

    if contains_vulnerable_unc_path(test_command):
        return False

    if contains_unquoted_expansion(test_command):
        return False

    if is_command_safe_via_flag_parsing(test_command):
        return True

    for regex in READONLY_COMMAND_REGEXES:
        if regex.search(test_command):
            # Block git -c / --exec-path / --config-env (config injection → code exec).
            if "git" in test_command and _GIT_C_RE.search(test_command):
                return False
            if "git" in test_command and _GIT_EXEC_PATH_RE.search(test_command):
                return False
            if "git" in test_command and _GIT_CONFIG_ENV_RE.search(test_command):
                return False
            return True
    return False


def _command_has_any_git(command: str) -> bool:
    # Lazy import: bash_permissions is a cyclic sibling.
    from tabvis.agent.tools.bash_permissions import is_normalized_git_command

    return any(
        is_normalized_git_command(subcmd.strip())
        for subcmd in split_command_deprecated(command)
    )


# Git-internal path patterns exploitable for sandbox escape.
GIT_INTERNAL_PATTERNS = [
    re.compile(r"^HEAD$"),
    re.compile(r"^objects(?:/|$)"),
    re.compile(r"^refs(?:/|$)"),
    re.compile(r"^hooks(?:/|$)"),
]

_LEADING_DOTSLASH_RE = re.compile(r"^\.?/")


def _is_git_internal_path(path: str) -> bool:
    normalized = _LEADING_DOTSLASH_RE.sub("", path)
    return any(pattern.search(normalized) for pattern in GIT_INTERNAL_PATTERNS)


# Commands that only delete or modify in-place (don't create new files at new paths).
NON_CREATING_WRITE_COMMANDS = {"rm", "rmdir", "sed"}


def _extract_write_paths_from_subcommand(subcommand: str) -> list[str]:
    parse_result = try_parse_shell_command(subcommand, lambda env: f"${env}")
    if not parse_result["success"]:
        return []

    tokens = [t for t in parse_result["tokens"] if isinstance(t, str)]
    if len(tokens) == 0:
        return []

    base_cmd = tokens[0]
    if not base_cmd:
        return []

    if base_cmd not in COMMAND_OPERATION_TYPE:
        return []
    op_type = COMMAND_OPERATION_TYPE[base_cmd]
    if (op_type not in ("write", "create")) or base_cmd in NON_CREATING_WRITE_COMMANDS:
        return []

    extractor = PATH_EXTRACTORS.get(base_cmd)
    if not extractor:
        return []

    return extractor(tokens[1:])


def _command_writes_to_git_internal_paths(command: str) -> bool:
    subcommands = split_command_deprecated(command)

    for subcmd in subcommands:
        trimmed = subcmd.strip()

        write_paths = _extract_write_paths_from_subcommand(trimmed)
        for path in write_paths:
            if _is_git_internal_path(path):
                return True

        redirect_result = extract_output_redirections(trimmed)
        for redirection in redirect_result["redirections"]:
            if _is_git_internal_path(redirection["target"]):
                return True

    return False


def is_current_directory_bare_git_repo() -> bool:
    """Bare/exploited repo detection — inlined here using ``os.stat`` against the live cwd."""
    cwd = get_cwd()

    git_path = os.path.join(cwd, ".git")
    try:
        st = os.stat(git_path)
        import stat as _stat

        if _stat.S_ISREG(st.st_mode):
            return False  # worktree/submodule gitdir reference
        if _stat.S_ISDIR(st.st_mode):
            git_head_path = os.path.join(git_path, "HEAD")
            try:
                head_st = os.stat(git_head_path)
                if _stat.S_ISREG(head_st.st_mode):
                    return False  # normal repo — valid .git/HEAD
            except OSError:
                pass
    except OSError:
        pass

    # No valid .git/HEAD — check cwd for bare repo indicators.
    try:
        if os.path.isfile(os.path.join(cwd, "HEAD")):
            return True
    except OSError:
        pass
    try:
        if os.path.isdir(os.path.join(cwd, "objects")):
            return True
    except OSError:
        pass
    try:
        if os.path.isdir(os.path.join(cwd, "refs")):
            return True
    except OSError:
        pass
    return False


def check_read_only_constraints(
    input: Any,
    compound_command_has_cd: bool,
) -> dict:
    """Decide whether a bash command is purely read-only (auto-allowable).

    Returns a ``PermissionResult`` dict: ``allow`` (with ``updatedInput``) when read-only,
    ``ask`` for UNC paths, else ``passthrough`` (defer to other permission checks).
    """
    command = input.command if not isinstance(input, dict) else input["command"]

    # Unparseable command → defer.
    result = try_parse_shell_command(command, lambda env: f"${env}")
    if not result["success"]:
        return {
            "behavior": "passthrough",
            "message": "Command cannot be parsed, requires further permission checks",
        }

    # Safety pre-check on the ORIGINAL command (before splitting transforms it).
    if bash_command_is_safe_deprecated(command).get("behavior") != "passthrough":
        return {
            "behavior": "passthrough",
            "message": "Command is not read-only, requires further permission checks",
        }

    # UNC paths in the original command (before backslash-transforming split).
    if contains_vulnerable_unc_path(command):
        return {
            "behavior": "ask",
            "message": (
                "Command contains Windows UNC path that could be vulnerable to WebDAV attacks"
            ),
        }

    has_git_command = _command_has_any_git(command)

    # Block compound commands with both cd AND git (sandbox escape via fake hooks).
    if compound_command_has_cd and has_git_command:
        return {
            "behavior": "passthrough",
            "message": (
                "Compound commands with cd and git require permission checks for enhanced security"
            ),
        }

    # Block git in directories that look like a bare/exploited git repo.
    if has_git_command and is_current_directory_bare_git_repo():
        return {
            "behavior": "passthrough",
            "message": (
                "Git commands in directories with bare repository structure require permission "
                "checks for enhanced security"
            ),
        }

    # Block compound commands that write git-internal paths AND run git.
    if has_git_command and _command_writes_to_git_internal_paths(command):
        return {
            "behavior": "passthrough",
            "message": (
                "Compound commands that create git internal files and run git require permission "
                "checks for enhanced security"
            ),
        }

    # Only auto-allow git as read-only if we're in the original cwd or sandbox is disabled.
    if (
        has_git_command
        and SandboxManager.is_sandboxing_enabled()
        and get_cwd() != get_original_cwd()
    ):
        return {
            "behavior": "passthrough",
            "message": (
                "Git commands outside the original working directory require permission checks "
                "when sandbox is enabled"
            ),
        }

    # Check if all subcommands are read-only.
    def _subcmd_read_only(subcmd: str) -> bool:
        if bash_command_is_safe_deprecated(subcmd).get("behavior") != "passthrough":
            return False
        return is_command_read_only(subcmd)

    all_subcommands_read_only = all(
        _subcmd_read_only(subcmd) for subcmd in split_command_deprecated(command)
    )

    if all_subcommands_read_only:
        return {"behavior": "allow", "updatedInput": input}

    return {
        "behavior": "passthrough",
        "message": "Command is not read-only, requires further permission checks",
    }
