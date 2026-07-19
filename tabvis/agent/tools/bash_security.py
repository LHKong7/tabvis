"""Bash security pre-parse checks

The legacy regex/shell-quote security path. This is the fallback used when tree-sitter is
unavailable; the primary gate is ``parse_for_security`` in ``tabvis.utils.bash.ast``. Both the
sync (:func:`bash_command_is_safe_deprecated`) and tree-sitter-aware async
(:func:`bash_command_is_safe_async_deprecated`) entry points are kept behaviorally equivalent.

The public surface BashTool consumers import:

  * :func:`bash_command_is_safe_deprecated` (sync) — ``bashCommandIsSafe_DEPRECATED``
  * :func:`bash_command_is_safe_async_deprecated` (async) — ``bashCommandIsSafeAsync_DEPRECATED``
  * :func:`strip_safe_heredoc_substitutions` — ``stripSafeHeredocSubstitutions``
  * :func:`has_safe_heredoc_substitution` — ``hasSafeHeredocSubstitution``

Casing: Python identifiers are snake_case; the returned :data:`PermissionResult` dicts keep
their camelCase wire keys (``updatedInput``, ``decisionReason``, ``isBashSecurityCheckForMisparsing``)
because they round-trip into the permission/transcript layer.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from tabvis.utils.bash.heredoc import extract_heredocs
from tabvis.utils.bash.parsed_command import ParsedCommand
from tabvis.utils.bash.shell_quote import (
    has_malformed_tokens,
    has_shell_quote_single_quote_bug,
    try_parse_shell_command,
)

if TYPE_CHECKING:
    from tabvis.types.permissions import PermissionResult
    from tabvis.utils.bash.tree_sitter_analysis import TreeSitterAnalysis

# A PermissionResult here is a plain dict (TypedDict union keyed by ``behavior``).
PermissionResult = dict  # noqa: F811 — runtime alias for the TYPE_CHECKING import

HEREDOC_IN_SUBSTITUTION = re.compile(r"\$\(.*<<")

# Note: Backtick pattern is handled separately in validate_dangerous_patterns
# to distinguish between escaped and unescaped backticks
COMMAND_SUBSTITUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"=\("), "Zsh process substitution =()"),
    # Zsh EQUALS expansion: =cmd at word start expands to $(which cmd).
    # `=curl evil.com` -> `/usr/bin/curl evil.com`, bypassing Bash(curl:*) deny
    # rules since the parser sees `=curl` as the base command, not `curl`.
    # Only matches word-initial = followed by a command-name char (not VAR=val).
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "Zsh equals expansion (=cmd)"),
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"\$\{"), "${} parameter substitution"),
    (re.compile(r"\$\["), "$[] legacy arithmetic expansion"),
    (re.compile(r"~\["), "Zsh-style parameter expansion"),
    (re.compile(r"\(e:"), "Zsh-style glob qualifiers"),
    (re.compile(r"\(\+"), "Zsh glob qualifier with command execution"),
    (re.compile(r"\}\s*always\s*\{"), "Zsh always block (try/always construct)"),
    # Defense in depth: Block PowerShell comment syntax even though we don't
    # execute in PowerShell. Protection against future changes.
    (re.compile(r"<#"), "PowerShell comment syntax"),
]

# Zsh-specific dangerous commands that can bypass security checks.
# These are checked against the base command (first word) of each command segment.
ZSH_DANGEROUS_COMMANDS = frozenset(
    {
        "zmodload",
        "emulate",
        "sysopen",
        "sysread",
        "syswrite",
        "sysseek",
        "zpty",
        "ztcp",
        "zsocket",
        "mapfile",
        "zf_rm",
        "zf_mv",
        "zf_ln",
        "zf_chmod",
        "zf_chown",
        "zf_mkdir",
        "zf_rmdir",
        "zf_chgrp",
    }
)

# Numeric identifiers for bash security checks (to avoid logging strings)
BASH_SECURITY_CHECK_IDS: dict[str, int] = {
    "INCOMPLETE_COMMANDS": 1,
    "JQ_SYSTEM_FUNCTION": 2,
    "JQ_FILE_ARGUMENTS": 3,
    "OBFUSCATED_FLAGS": 4,
    "SHELL_METACHARACTERS": 5,
    "DANGEROUS_VARIABLES": 6,
    "NEWLINES": 7,
    "DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION": 8,
    "DANGEROUS_PATTERNS_INPUT_REDIRECTION": 9,
    "DANGEROUS_PATTERNS_OUTPUT_REDIRECTION": 10,
    "IFS_INJECTION": 11,
    "GIT_COMMIT_SUBSTITUTION": 12,
    "PROC_ENVIRON_ACCESS": 13,
    "MALFORMED_TOKEN_INJECTION": 14,
    "BACKSLASH_ESCAPED_WHITESPACE": 15,
    "BRACE_EXPANSION": 16,
    "CONTROL_CHARACTERS": 17,
    "UNICODE_WHITESPACE": 18,
    "MID_WORD_HASH": 19,
    "ZSH_DANGEROUS_COMMANDS": 20,
    "BACKSLASH_ESCAPED_OPERATORS": 21,
    "COMMENT_QUOTE_DESYNC": 22,
    "QUOTED_NEWLINE": 23,
}


class ValidationContext(dict):
    """Validation context — a plain dict with the TS field names (snake_case identifiers).

    Fields: ``original_command``, ``base_command``, ``unquoted_content``,
    ``fully_unquoted_content``, ``fully_unquoted_pre_strip``, ``unquoted_keep_quote_chars``,
    optional ``tree_sitter``.
    """

    @property
    def original_command(self) -> str:
        return self["original_command"]

    @property
    def base_command(self) -> str:
        return self["base_command"]

    @property
    def unquoted_content(self) -> str:
        return self["unquoted_content"]

    @property
    def fully_unquoted_content(self) -> str:
        return self["fully_unquoted_content"]

    @property
    def fully_unquoted_pre_strip(self) -> str:
        return self["fully_unquoted_pre_strip"]

    @property
    def unquoted_keep_quote_chars(self) -> str:
        return self["unquoted_keep_quote_chars"]

    @property
    def tree_sitter(self) -> TreeSitterAnalysis | None:
        return self.get("tree_sitter")


def _log(check_id: int, sub_id: int | None = None) -> None:
    metadata: dict[str, Any] = {"checkId": check_id}
    if sub_id is not None:
        metadata["subId"] = sub_id


def extract_quoted_content(command: str, is_jq: bool = False) -> dict[str, str]:
    """Extract the quoted content.

    Returns ``{'withDoubleQuotes', 'fullyUnquoted', 'unquotedKeepQuoteChars'}``.
    """
    with_double_quotes = ""
    fully_unquoted = ""
    unquoted_keep_quote_chars = ""
    in_single_quote = False
    in_double_quote = False
    escaped = False

    i = 0
    length = len(command)
    while i < length:
        char = command[i]

        if escaped:
            escaped = False
            if not in_single_quote:
                with_double_quotes += char
            if not in_single_quote and not in_double_quote:
                fully_unquoted += char
            if not in_single_quote and not in_double_quote:
                unquoted_keep_quote_chars += char
            i += 1
            continue

        if char == "\\" and not in_single_quote:
            escaped = True
            if not in_single_quote:
                with_double_quotes += char
            if not in_single_quote and not in_double_quote:
                fully_unquoted += char
            if not in_single_quote and not in_double_quote:
                unquoted_keep_quote_chars += char
            i += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            unquoted_keep_quote_chars += char
            i += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            unquoted_keep_quote_chars += char
            # For jq, include quotes in extraction to ensure content is analyzed.
            if not is_jq:
                i += 1
                continue

        if not in_single_quote:
            with_double_quotes += char
        if not in_single_quote and not in_double_quote:
            fully_unquoted += char
        if not in_single_quote and not in_double_quote:
            unquoted_keep_quote_chars += char
        i += 1

    return {
        "withDoubleQuotes": with_double_quotes,
        "fullyUnquoted": fully_unquoted,
        "unquotedKeepQuoteChars": unquoted_keep_quote_chars,
    }


_REDIR_2GT1_RE = re.compile(r"\s+2\s*>&\s*1(?=\s|$)")
_REDIR_DEVNULL_OUT_RE = re.compile(r"[012]?\s*>\s*/dev/null(?=\s|$)")
_REDIR_DEVNULL_IN_RE = re.compile(r"\s*<\s*/dev/null(?=\s|$)")


def strip_safe_redirections(content: str) -> str:
    """Remove redirections that are safe for command analysis.

    SECURITY: All three patterns MUST have a trailing boundary ``(?=\\s|$)``.
    """
    content = _REDIR_2GT1_RE.sub("", content)
    content = _REDIR_DEVNULL_OUT_RE.sub("", content)
    content = _REDIR_DEVNULL_IN_RE.sub("", content)
    return content


def has_unescaped_char(content: str, char: str) -> bool:
    """Unescaped occurrence of a single character."""
    if len(char) != 1:
        raise ValueError("has_unescaped_char only works with single characters")

    i = 0
    length = len(content)
    while i < length:
        # Backslash escapes the next character.
        if content[i] == "\\" and i + 1 < length:
            i += 2
            continue
        if content[i] == char:
            return True
        i += 1
    return False


def validate_empty(context: ValidationContext) -> PermissionResult:
    if not context.original_command.strip():
        return {
            "behavior": "allow",
            "updatedInput": {"command": context.original_command},
            "decisionReason": {"type": "other", "reason": "Empty command is safe"},
        }
    return {"behavior": "passthrough", "message": "Command is not empty"}


_TAB_START_RE = re.compile(r"^\s*\t")
_OPERATOR_START_RE = re.compile(r"^\s*(&&|\|\||;|>>?|<)")


def validate_incomplete_commands(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command
    trimmed = original_command.strip()

    if _TAB_START_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["INCOMPLETE_COMMANDS"], 1)
        return {
            "behavior": "ask",
            "message": "Command appears to be an incomplete fragment (starts with tab)",
        }

    if trimmed.startswith("-"):
        _log(BASH_SECURITY_CHECK_IDS["INCOMPLETE_COMMANDS"], 2)
        return {
            "behavior": "ask",
            "message": "Command appears to be an incomplete fragment (starts with flags)",
        }

    if _OPERATOR_START_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["INCOMPLETE_COMMANDS"], 3)
        return {
            "behavior": "ask",
            "message": "Command appears to be a continuation line (starts with operator)",
        }

    return {"behavior": "passthrough", "message": "Command appears complete"}


_HEREDOC_PATTERN = re.compile(
    r"\$\(cat[ \t]*<<(-?)[ \t]*(?:'+([A-Za-z_]\w*)'+|\\([A-Za-z_]\w*))"
)
_OPEN_LINE_TAIL_RE = re.compile(r"^[ \t]*$")
_PAREN_LEAD_RE = re.compile(r"^([ \t]*)\)")
_EOF_METACHAR_AFTER_DELIM_RE = re.compile(r"^[)}`|&;(<>]")
_REMAINING_SAFE_CHARS_RE = re.compile(r"^[a-zA-Z0-9 \t\"'.\-/_@=,:+~]*$")
_LEADING_TABS_RE = re.compile(r"^\t*")


def is_safe_heredoc(command: str) -> bool:
    """Provably-safe ``$(cat <<'DELIM'...DELIM)`` pattern."""
    if not HEREDOC_IN_SUBSTITUTION.search(command):
        return False

    safe_heredocs: list[dict[str, Any]] = []
    for match in _HEREDOC_PATTERN.finditer(command):
        delimiter = match.group(2) or match.group(3)
        if delimiter:
            safe_heredocs.append(
                {
                    "start": match.start(),
                    "operator_end": match.start() + len(match.group(0)),
                    "delimiter": delimiter,
                    "is_dash": match.group(1) == "-",
                }
            )

    if len(safe_heredocs) == 0:
        return False

    verified: list[dict[str, int]] = []

    for hd in safe_heredocs:
        start = hd["start"]
        operator_end = hd["operator_end"]
        delimiter = hd["delimiter"]
        is_dash = hd["is_dash"]

        after_operator = command[operator_end:]
        open_line_end = after_operator.find("\n")
        if open_line_end == -1:
            return False  # No content at all
        open_line_tail = after_operator[:open_line_end]
        if not _OPEN_LINE_TAIL_RE.match(open_line_tail):
            return False  # Extra content on open line

        body_start = operator_end + open_line_end + 1
        body = command[body_start:]
        body_lines = body.split("\n")

        closing_line_idx = -1
        close_paren_line_idx = -1
        close_paren_col_idx = -1

        for i in range(len(body_lines)):
            raw_line = body_lines[i]
            line = re.sub(r"^\t*", "", raw_line) if is_dash else raw_line

            # Form 1: delimiter alone on a line
            if line == delimiter:
                closing_line_idx = i
                if i + 1 >= len(body_lines):
                    return False  # No closing `)`
                next_line = body_lines[i + 1]
                paren_match = _PAREN_LEAD_RE.match(next_line)
                if not paren_match:
                    return False  # `)` not at start of next line
                close_paren_line_idx = i + 1
                close_paren_col_idx = len(paren_match.group(1))
                break

            # Form 2: delimiter immediately followed by `)` (PST_EOFTOKEN form)
            if line.startswith(delimiter):
                after_delim = line[len(delimiter) :]
                paren_match = _PAREN_LEAD_RE.match(after_delim)
                if paren_match:
                    closing_line_idx = i
                    close_paren_line_idx = i
                    tab_prefix_match = _LEADING_TABS_RE.match(raw_line) if is_dash else None
                    tab_prefix = tab_prefix_match.group(0) if tab_prefix_match else ""
                    close_paren_col_idx = (
                        len(tab_prefix) + len(delimiter) + len(paren_match.group(1))
                    )
                    break
                # Line starts with delimiter but has other trailing content.
                if _EOF_METACHAR_AFTER_DELIM_RE.match(after_delim):
                    return False  # Ambiguous early-closure pattern

        if closing_line_idx == -1:
            return False  # No closing delimiter found

        end_pos = body_start
        for i in range(close_paren_line_idx):
            end_pos += len(body_lines[i]) + 1  # +1 for newline
        end_pos += close_paren_col_idx + 1  # +1 to include the `)` itself

        verified.append({"start": start, "end": end_pos})

    # Reject nested matches.
    for outer in verified:
        for inner in verified:
            if inner is outer:
                continue
            if inner["start"] > outer["start"] and inner["start"] < outer["end"]:
                return False

    # Strip all verified heredocs (reverse order so earlier indices stay valid).
    sorted_verified = sorted(verified, key=lambda v: v["start"], reverse=True)
    remaining = command
    for v in sorted_verified:
        remaining = remaining[: v["start"]] + remaining[v["end"] :]

    trimmed_remaining = remaining.strip()
    if len(trimmed_remaining) > 0:
        first_heredoc_start = min(v["start"] for v in verified)
        prefix = command[:first_heredoc_start]
        if len(prefix.strip()) == 0:
            return False

    if not _REMAINING_SAFE_CHARS_RE.match(remaining):
        return False

    if bash_command_is_safe_deprecated(remaining).get("behavior") != "passthrough":
        return False

    return True


_INNER_PAREN_RE = re.compile(r"^[ \t]*\)")


def strip_safe_heredoc_substitutions(command: str) -> str | None:
    """Remove safe command substitutions embedded in heredocs.

    Strips well-formed ``$(cat <<'DELIM'...DELIM)`` heredoc substitutions, returning the
    remaining command, or ``None`` if none found.
    """
    if not HEREDOC_IN_SUBSTITUTION.search(command):
        return None

    result = command
    found = False
    ranges: list[dict[str, int]] = []

    for match in _HEREDOC_PATTERN.finditer(command):
        if match.start() > 0 and command[match.start() - 1] == "\\":
            continue
        delimiter = match.group(2) or match.group(3)
        if not delimiter:
            continue
        is_dash = match.group(1) == "-"
        operator_end = match.start() + len(match.group(0))

        after_operator = command[operator_end:]
        open_line_end = after_operator.find("\n")
        if open_line_end == -1:
            continue
        if not _OPEN_LINE_TAIL_RE.match(after_operator[:open_line_end]):
            continue

        body_start = operator_end + open_line_end + 1
        body_lines = command[body_start:].split("\n")
        for i in range(len(body_lines)):
            raw_line = body_lines[i]
            line = re.sub(r"^\t*", "", raw_line) if is_dash else raw_line
            if line.startswith(delimiter):
                after = line[len(delimiter) :]
                close_pos = -1
                if _INNER_PAREN_RE.match(after):
                    line_start = (
                        body_start
                        + len("\n".join(body_lines[:i]))
                        + (1 if i > 0 else 0)
                    )
                    close_pos = command.find(")", line_start)
                elif after == "":
                    next_line = body_lines[i + 1] if i + 1 < len(body_lines) else None
                    if next_line is not None and _INNER_PAREN_RE.match(next_line):
                        next_line_start = (
                            body_start + len("\n".join(body_lines[: i + 1])) + 1
                        )
                        close_pos = command.find(")", next_line_start)
                if close_pos != -1:
                    ranges.append({"start": match.start(), "end": close_pos + 1})
                    found = True
                break

    if not found:
        return None
    for i in range(len(ranges) - 1, -1, -1):
        r = ranges[i]
        result = result[: r["start"]] + result[r["end"] :]
    return result


def has_safe_heredoc_substitution(command: str) -> bool:
    """Detection-only check: does the command contain a safe heredoc substitution?"""
    return strip_safe_heredoc_substitutions(command) is not None


def validate_safe_command_substitution(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    if not HEREDOC_IN_SUBSTITUTION.search(original_command):
        return {"behavior": "passthrough", "message": "No heredoc in substitution"}

    if is_safe_heredoc(original_command):
        return {
            "behavior": "allow",
            "updatedInput": {"command": original_command},
            "decisionReason": {
                "type": "other",
                "reason": "Safe command substitution: cat with quoted/escaped heredoc delimiter",
            },
        }

    return {
        "behavior": "passthrough",
        "message": "Command substitution needs validation",
    }


_GIT_COMMIT_PREFIX_RE = re.compile(r"^git\s+commit\s+")
_GIT_COMMIT_MSG_RE = re.compile(
    r"^git[ \t]+commit[ \t]+[^;&|`$<>()\n\r]*?-m[ \t]+([\"'])([\s\S]*?)\1(.*)$"
)
_SUBSTITUTION_IN_MSG_RE = re.compile(r"\$\(|`|\$\{")
_REMAINDER_METACHAR_RE = re.compile(r"[;|&()`]|\$\(|\$\{")
_REDIRECT_RE = re.compile(r"[<>]")


def validate_git_commit(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command
    base_command = context.base_command

    if base_command != "git" or not _GIT_COMMIT_PREFIX_RE.search(original_command):
        return {"behavior": "passthrough", "message": "Not a git commit"}

    if "\\" in original_command:
        return {
            "behavior": "passthrough",
            "message": "Git commit contains backslash, needs full validation",
        }

    message_match = _GIT_COMMIT_MSG_RE.match(original_command)
    if message_match:
        quote = message_match.group(1)
        message_content = message_match.group(2)
        remainder = message_match.group(3)

        if quote == '"' and message_content and _SUBSTITUTION_IN_MSG_RE.search(message_content):
            _log(BASH_SECURITY_CHECK_IDS["GIT_COMMIT_SUBSTITUTION"], 1)
            return {
                "behavior": "ask",
                "message": "Git commit message contains command substitution patterns",
            }

        if remainder and _REMAINDER_METACHAR_RE.search(remainder):
            return {
                "behavior": "passthrough",
                "message": "Git commit remainder contains shell metacharacters",
            }
        if remainder:
            unquoted = ""
            in_sq = False
            in_dq = False
            for c in remainder:
                if c == "'" and not in_dq:
                    in_sq = not in_sq
                    continue
                if c == '"' and not in_sq:
                    in_dq = not in_dq
                    continue
                if not in_sq and not in_dq:
                    unquoted += c
            if _REDIRECT_RE.search(unquoted):
                return {
                    "behavior": "passthrough",
                    "message": "Git commit remainder contains unquoted redirect operator",
                }

        if message_content and message_content.startswith("-"):
            _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 5)
            return {
                "behavior": "ask",
                "message": "Command contains quoted characters in flag names",
            }

        return {
            "behavior": "allow",
            "updatedInput": {"command": original_command},
            "decisionReason": {
                "type": "other",
                "reason": "Git commit with simple quoted message is allowed",
            },
        }

    return {"behavior": "passthrough", "message": "Git commit needs validation"}


_JQ_SYSTEM_RE = re.compile(r"\bsystem\s*\(")
_JQ_FILE_FLAGS_RE = re.compile(
    r"(?:^|\s)(?:-f\b|--from-file|--rawfile|--slurpfile|-L\b|--library-path)"
)


def validate_jq_command(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command
    base_command = context.base_command

    if base_command != "jq":
        return {"behavior": "passthrough", "message": "Not jq"}

    if _JQ_SYSTEM_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["JQ_SYSTEM_FUNCTION"], 1)
        return {
            "behavior": "ask",
            "message": "jq command contains system() function which executes arbitrary commands",
        }

    after_jq = original_command[3:].strip()
    if _JQ_FILE_FLAGS_RE.search(after_jq):
        _log(BASH_SECURITY_CHECK_IDS["JQ_FILE_ARGUMENTS"], 1)
        return {
            "behavior": "ask",
            "message": "jq command contains dangerous flags that could execute code or read arbitrary files",
        }

    return {"behavior": "passthrough", "message": "jq command is safe"}


_QUOTED_METACHAR_RE = re.compile(r"""(?:^|\s)["'][^"']*[;&][^"']*["'](?:\s|$)""")
_GLOB_NAME_RE = re.compile(r"""-name\s+["'][^"']*[;|&][^"']*["']""")
_GLOB_PATH_RE = re.compile(r"""-path\s+["'][^"']*[;|&][^"']*["']""")
_GLOB_INAME_RE = re.compile(r"""-iname\s+["'][^"']*[;|&][^"']*["']""")
_REGEX_METACHAR_RE = re.compile(r"""-regex\s+["'][^"']*[;&][^"']*["']""")


def validate_shell_metacharacters(context: ValidationContext) -> PermissionResult:
    unquoted_content = context.unquoted_content
    message = "Command contains shell metacharacters (;, |, or &) in arguments"

    if _QUOTED_METACHAR_RE.search(unquoted_content):
        _log(BASH_SECURITY_CHECK_IDS["SHELL_METACHARACTERS"], 1)
        return {"behavior": "ask", "message": message}

    if (
        _GLOB_NAME_RE.search(unquoted_content)
        or _GLOB_PATH_RE.search(unquoted_content)
        or _GLOB_INAME_RE.search(unquoted_content)
    ):
        _log(BASH_SECURITY_CHECK_IDS["SHELL_METACHARACTERS"], 2)
        return {"behavior": "ask", "message": message}

    if _REGEX_METACHAR_RE.search(unquoted_content):
        _log(BASH_SECURITY_CHECK_IDS["SHELL_METACHARACTERS"], 3)
        return {"behavior": "ask", "message": message}

    return {"behavior": "passthrough", "message": "No metacharacters"}


_VAR_IN_REDIR_RE = re.compile(r"[<>|]\s*\$[A-Za-z_]")
_VAR_BEFORE_REDIR_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\s*[|<>]")


def validate_dangerous_variables(context: ValidationContext) -> PermissionResult:
    fully_unquoted_content = context.fully_unquoted_content

    if _VAR_IN_REDIR_RE.search(fully_unquoted_content) or _VAR_BEFORE_REDIR_RE.search(
        fully_unquoted_content
    ):
        _log(BASH_SECURITY_CHECK_IDS["DANGEROUS_VARIABLES"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains variables in dangerous contexts (redirections or pipes)",
        }

    return {"behavior": "passthrough", "message": "No dangerous variables"}


def validate_dangerous_patterns(context: ValidationContext) -> PermissionResult:
    unquoted_content = context.unquoted_content

    # Backticks: check for UNESCAPED backticks only (escaped \` are safe).
    if has_unescaped_char(unquoted_content, "`"):
        return {
            "behavior": "ask",
            "message": "Command contains backticks (`) for command substitution",
        }

    for pattern, message in COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted_content):
            _log(BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_COMMAND_SUBSTITUTION"], 1)
            return {"behavior": "ask", "message": f"Command contains {message}"}

    return {"behavior": "passthrough", "message": "No dangerous patterns"}


def validate_redirections(context: ValidationContext) -> PermissionResult:
    fully_unquoted_content = context.fully_unquoted_content

    if "<" in fully_unquoted_content:
        _log(BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_INPUT_REDIRECTION"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains input redirection (<) which could read sensitive files",
        }

    if ">" in fully_unquoted_content:
        _log(BASH_SECURITY_CHECK_IDS["DANGEROUS_PATTERNS_OUTPUT_REDIRECTION"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains output redirection (>) which could write to arbitrary files",
        }

    return {"behavior": "passthrough", "message": "No redirections"}


_NEWLINE_RE = re.compile(r"[\n\r]")
# `\<NL>` continuation at word boundary is safe; bare `<NL>\S` looks like a command.
_LOOKS_LIKE_COMMAND_RE = re.compile(r"(?<![\s]\\)[\n\r]\s*\S")


def validate_newlines(context: ValidationContext) -> PermissionResult:
    fully_unquoted_pre_strip = context.fully_unquoted_pre_strip

    if not _NEWLINE_RE.search(fully_unquoted_pre_strip):
        return {"behavior": "passthrough", "message": "No newlines"}

    if _LOOKS_LIKE_COMMAND_RE.search(fully_unquoted_pre_strip):
        _log(BASH_SECURITY_CHECK_IDS["NEWLINES"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains newlines that could separate multiple commands",
        }

    return {"behavior": "passthrough", "message": "Newlines appear to be within data"}


def validate_carriage_return(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    if "\r" not in original_command:
        return {"behavior": "passthrough", "message": "No carriage return"}

    in_single_quote = False
    in_double_quote = False
    escaped = False
    for c in original_command:
        if escaped:
            escaped = False
            continue
        if c == "\\" and not in_single_quote:
            escaped = True
            continue
        if c == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if c == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if c == "\r" and not in_double_quote:
            _log(BASH_SECURITY_CHECK_IDS["NEWLINES"], 2)
            return {
                "behavior": "ask",
                "message": "Command contains carriage return (\\r) which shell-quote and bash tokenize differently",
            }

    return {"behavior": "passthrough", "message": "CR only inside double quotes"}


_IFS_RE = re.compile(r"\$IFS|\$\{[^}]*IFS")


def validate_ifs_injection(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    if _IFS_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["IFS_INJECTION"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains IFS variable usage which could bypass security validation",
        }

    return {"behavior": "passthrough", "message": "No IFS injection detected"}


_PROC_ENVIRON_RE = re.compile(r"/proc/.*/environ")


def validate_proc_environ_access(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    if _PROC_ENVIRON_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["PROC_ENVIRON_ACCESS"], 1)
        return {
            "behavior": "ask",
            "message": "Command accesses /proc/*/environ which could expose sensitive environment variables",
        }

    return {"behavior": "passthrough", "message": "No /proc/environ access detected"}


def validate_malformed_token_injection(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    parse_result = try_parse_shell_command(original_command)
    if not parse_result.get("success"):
        return {"behavior": "passthrough", "message": "Parse failed, handled elsewhere"}

    parsed = parse_result["tokens"]

    def _is_separator(entry: Any) -> bool:
        return (
            isinstance(entry, dict)
            and "op" in entry
            and entry["op"] in (";", "&&", "||")
        )

    has_command_separator = any(_is_separator(entry) for entry in parsed)

    if not has_command_separator:
        return {"behavior": "passthrough", "message": "No command separators"}

    if has_malformed_tokens(original_command, parsed):
        _log(BASH_SECURITY_CHECK_IDS["MALFORMED_TOKEN_INJECTION"], 1)
        return {
            "behavior": "ask",
            "message": "Command contains ambiguous syntax with command separators that could be misinterpreted",
        }

    return {"behavior": "passthrough", "message": "No malformed token injection detected"}


_SHELL_OPERATORS_RE = re.compile(r"[|&;]")
_ANSI_C_QUOTE_RE = re.compile(r"\$'[^']*'")
_LOCALE_QUOTE_RE = re.compile(r'\$"[^"]*"')
_EMPTY_SPECIAL_DASH_RE = re.compile(r"""\$['"]{2}\s*-""")
_EMPTY_QUOTE_DASH_RE = re.compile(r"""(?:^|\s)(?:''|"")+\s*-""")
_HOMOG_EMPTY_DASH_RE = re.compile(r"""(?:""|'')+['"]-""")
_TRIPLE_QUOTE_RE = re.compile(r"""(?:^|\s)['"]{3,}""")
_WHITESPACE_RE = re.compile(r"\s")
_QUOTE_CHARS_RE = re.compile(r"""['"`]""")
_FLAG_INSIDE_RE = re.compile(r"^-+[a-zA-Z0-9$`]")
_ALL_DASHES_RE = re.compile(r"^-+$")
_FLAG_CONTINUATION_CHARS_RE = re.compile(r"[a-zA-Z0-9\\${`-]")
_FLAG_COMBINED_RE = re.compile(r"^-+[a-zA-Z0-9$`]")
_ALNUM_EXPAND_RE = re.compile(r"[a-zA-Z0-9$`]")
_STARTS_DASH_RE = re.compile(r"^-")
_ALNUM_BACKSLASH_EXPAND_RE = re.compile(r"[a-zA-Z0-9\\${`]")
_QUOTE_SP_DASH_RE = re.compile(r"""\s['"`]-""")
_DOUBLE_QUOTE_DASH_RE = re.compile(r"""['"`]{2}-""")
_FLAG_END_RE = re.compile(r"[\s=]")
_FLAG_VALID_CONT_RE = re.compile(r"[a-zA-Z0-9_'\"-]")


def validate_obfuscated_flags(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command
    base_command = context.base_command

    has_shell_operators = bool(_SHELL_OPERATORS_RE.search(original_command))
    if base_command == "echo" and not has_shell_operators:
        return {
            "behavior": "passthrough",
            "message": "echo command is safe and has no dangerous flags",
        }

    if _ANSI_C_QUOTE_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 5)
        return {
            "behavior": "ask",
            "message": "Command contains ANSI-C quoting which can hide characters",
        }

    if _LOCALE_QUOTE_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 6)
        return {
            "behavior": "ask",
            "message": "Command contains locale quoting which can hide characters",
        }

    if _EMPTY_SPECIAL_DASH_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 9)
        return {
            "behavior": "ask",
            "message": "Command contains empty special quotes before dash (potential bypass)",
        }

    if _EMPTY_QUOTE_DASH_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 7)
        return {
            "behavior": "ask",
            "message": "Command contains empty quotes before dash (potential bypass)",
        }

    if _HOMOG_EMPTY_DASH_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 10)
        return {
            "behavior": "ask",
            "message": "Command contains empty quote pair adjacent to quoted dash (potential flag obfuscation)",
        }

    if _TRIPLE_QUOTE_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 11)
        return {
            "behavior": "ask",
            "message": "Command contains consecutive quote characters at word start (potential obfuscation)",
        }

    # Track quote state to avoid false positives for flags inside quoted strings.
    in_single_quote = False
    in_double_quote = False
    escaped = False

    cmd_len = len(original_command)
    for i in range(cmd_len - 1):
        current_char = original_command[i]
        next_char = original_command[i + 1]

        if escaped:
            escaped = False
            continue

        if current_char == "\\" and not in_single_quote:
            escaped = True
            continue

        if current_char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if current_char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if in_single_quote or in_double_quote:
            continue

        # Whitespace followed by quote containing a dash (flag obfuscation).
        if (
            current_char
            and next_char
            and _WHITESPACE_RE.match(current_char)
            and _QUOTE_CHARS_RE.match(next_char)
        ):
            quote_char = next_char
            j = i + 2
            inside_quote = ""
            while j < cmd_len and original_command[j] != quote_char:
                inside_quote += original_command[j]
                j += 1

            char_after_quote = (
                original_command[j + 1] if j + 1 < cmd_len else None
            )
            has_flag_chars_inside = bool(_FLAG_INSIDE_RE.match(inside_quote))
            has_flag_chars_continuing = (
                bool(_ALL_DASHES_RE.match(inside_quote))
                and char_after_quote is not None
                and bool(_FLAG_CONTINUATION_CHARS_RE.match(char_after_quote))
            )
            has_flag_chars_in_next_quote = _check_flag_chars_in_next_quote(
                original_command, inside_quote, char_after_quote, j, cmd_len
            )
            if (
                j < cmd_len
                and original_command[j] == quote_char
                and (
                    has_flag_chars_inside
                    or has_flag_chars_continuing
                    or has_flag_chars_in_next_quote
                )
            ):
                _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 4)
                return {
                    "behavior": "ask",
                    "message": "Command contains quoted characters in flag names",
                }

        # Whitespace followed by dash - this starts a flag.
        if (
            current_char
            and next_char
            and _WHITESPACE_RE.match(current_char)
            and next_char == "-"
        ):
            j = i + 1
            flag_content = ""
            while j < cmd_len:
                flag_char = original_command[j]
                if not flag_char:
                    break
                if _FLAG_END_RE.match(flag_char):
                    break
                if _QUOTE_CHARS_RE.match(flag_char):
                    if (
                        base_command == "cut"
                        and flag_content == "-d"
                        and _QUOTE_CHARS_RE.match(flag_char)
                    ):
                        break
                    if j + 1 < cmd_len:
                        next_flag_char = original_command[j + 1]
                        if next_flag_char and not _FLAG_VALID_CONT_RE.match(
                            next_flag_char
                        ):
                            break
                flag_content += flag_char
                j += 1

            if '"' in flag_content or "'" in flag_content:
                _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 1)
                return {
                    "behavior": "ask",
                    "message": "Command contains quoted characters in flag names",
                }

    # Flags that start with quotes: "--"output, '-'-output, etc.
    if _QUOTE_SP_DASH_RE.search(context.fully_unquoted_content):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 2)
        return {
            "behavior": "ask",
            "message": "Command contains quoted characters in flag names",
        }

    # Cases like ""--output
    if _DOUBLE_QUOTE_DASH_RE.search(context.fully_unquoted_content):
        _log(BASH_SECURITY_CHECK_IDS["OBFUSCATED_FLAGS"], 3)
        return {
            "behavior": "ask",
            "message": "Command contains quoted characters in flag names",
        }

    return {"behavior": "passthrough", "message": "No obfuscated flags detected"}


def _check_flag_chars_in_next_quote(
    original_command: str,
    inside_quote: str,
    char_after_quote: str | None,
    j: int,
    cmd_len: int,
) -> bool:
    """Return whether flag chars in next quote."""
    if not (inside_quote == "" or _ALL_DASHES_RE.match(inside_quote)):
        return False
    if char_after_quote is None:
        return False
    if not _QUOTE_CHARS_RE.match(char_after_quote):
        return False

    pos = j + 1  # Start at char_after_quote (an opening quote)
    combined_content = inside_quote
    while pos < cmd_len and _QUOTE_CHARS_RE.match(original_command[pos]):
        seg_quote = original_command[pos]
        end = pos + 1
        while end < cmd_len and original_command[end] != seg_quote:
            end += 1
        segment = original_command[pos + 1 : end]
        combined_content += segment

        if _FLAG_COMBINED_RE.match(combined_content):
            return True

        prior_content = (
            combined_content[: -len(segment)] if len(segment) > 0 else combined_content
        )
        if _ALL_DASHES_RE.match(prior_content):
            if _ALNUM_EXPAND_RE.search(segment):
                return True

        if end >= cmd_len:
            break
        pos = end + 1

    # Also check the unquoted char at the end of the chain.
    if pos < cmd_len and _FLAG_CONTINUATION_CHARS_RE.match(original_command[pos]):
        if _ALL_DASHES_RE.match(combined_content) or combined_content == "":
            next_char = original_command[pos]
            if next_char == "-":
                return True
            if _ALNUM_BACKSLASH_EXPAND_RE.match(next_char) and combined_content != "":
                return True
        if _STARTS_DASH_RE.match(combined_content):
            return True
    return False


def has_backslash_escaped_whitespace(command: str) -> bool:
    """Return whether backslash escaped whitespace."""
    in_single_quote = False
    in_double_quote = False

    i = 0
    length = len(command)
    while i < length:
        char = command[i]

        if char == "\\" and not in_single_quote:
            if not in_double_quote:
                next_char = command[i + 1] if i + 1 < length else None
                if next_char == " " or next_char == "\t":
                    return True
            i += 1
            i += 1
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            i += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            i += 1
            continue

        i += 1

    return False


def validate_backslash_escaped_whitespace(context: ValidationContext) -> PermissionResult:
    if has_backslash_escaped_whitespace(context.original_command):
        _log(BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_WHITESPACE"])
        return {
            "behavior": "ask",
            "message": "Command contains backslash-escaped whitespace that could alter command parsing",
        }

    return {"behavior": "passthrough", "message": "No backslash-escaped whitespace"}


SHELL_OPERATORS = frozenset({";", "|", "&", "<", ">"})


def has_backslash_escaped_operator(command: str) -> bool:
    """Return whether backslash escaped operator."""
    in_single_quote = False
    in_double_quote = False

    i = 0
    length = len(command)
    while i < length:
        char = command[i]

        if char == "\\" and not in_single_quote:
            if not in_double_quote:
                next_char = command[i + 1] if i + 1 < length else None
                if next_char and next_char in SHELL_OPERATORS:
                    return True
            i += 1
            i += 1
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            i += 1
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            i += 1
            continue

        i += 1

    return False


def validate_backslash_escaped_operators(context: ValidationContext) -> PermissionResult:
    tree_sitter = context.tree_sitter
    if tree_sitter and not tree_sitter.get("hasActualOperatorNodes"):
        return {"behavior": "passthrough", "message": "No operator nodes in AST"}

    if has_backslash_escaped_operator(context.original_command):
        _log(BASH_SECURITY_CHECK_IDS["BACKSLASH_ESCAPED_OPERATORS"])
        return {
            "behavior": "ask",
            "message": "Command contains a backslash before a shell operator (;, |, &, <, >) which can hide command structure",
        }

    return {"behavior": "passthrough", "message": "No backslash-escaped operators"}


def is_escaped_at_position(content: str, pos: int) -> bool:
    """Odd run of backslashes before ``pos``."""
    backslash_count = 0
    i = pos - 1
    while i >= 0 and content[i] == "\\":
        backslash_count += 1
        i -= 1
    return backslash_count % 2 == 1


_QUOTED_SINGLE_BRACE_RE = re.compile(r"""['"][{}]['"]""")


def validate_brace_expansion(context: ValidationContext) -> PermissionResult:
    content = context.fully_unquoted_pre_strip

    unescaped_open_braces = 0
    unescaped_close_braces = 0
    for i in range(len(content)):
        if content[i] == "{" and not is_escaped_at_position(content, i):
            unescaped_open_braces += 1
        elif content[i] == "}" and not is_escaped_at_position(content, i):
            unescaped_close_braces += 1

    if unescaped_open_braces > 0 and unescaped_close_braces > unescaped_open_braces:
        _log(BASH_SECURITY_CHECK_IDS["BRACE_EXPANSION"], 2)
        return {
            "behavior": "ask",
            "message": "Command has excess closing braces after quote stripping, indicating possible brace expansion obfuscation",
        }

    if unescaped_open_braces > 0:
        orig = context.original_command
        if _QUOTED_SINGLE_BRACE_RE.search(orig):
            _log(BASH_SECURITY_CHECK_IDS["BRACE_EXPANSION"], 3)
            return {
                "behavior": "ask",
                "message": "Command contains quoted brace character inside brace context (potential brace expansion obfuscation)",
            }

    for i in range(len(content)):
        if content[i] != "{":
            continue
        if is_escaped_at_position(content, i):
            continue

        depth = 1
        matching_close = -1
        for j in range(i + 1, len(content)):
            ch = content[j]
            if ch == "{" and not is_escaped_at_position(content, j):
                depth += 1
            elif ch == "}" and not is_escaped_at_position(content, j):
                depth -= 1
                if depth == 0:
                    matching_close = j
                    break

        if matching_close == -1:
            continue

        inner_depth = 0
        for k in range(i + 1, matching_close):
            ch = content[k]
            if ch == "{" and not is_escaped_at_position(content, k):
                inner_depth += 1
            elif ch == "}" and not is_escaped_at_position(content, k):
                inner_depth -= 1
            elif inner_depth == 0:
                if ch == "," or (
                    ch == "." and k + 1 < matching_close and content[k + 1] == "."
                ):
                    _log(BASH_SECURITY_CHECK_IDS["BRACE_EXPANSION"], 1)
                    return {
                        "behavior": "ask",
                        "message": "Command contains brace expansion that could alter command parsing",
                    }

    return {"behavior": "passthrough", "message": "No brace expansion detected"}


# Unicode whitespace that shell-quote splits on but bash treats as literal.
UNICODE_WS_RE = re.compile(
    "[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
)


def validate_unicode_whitespace(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command
    if UNICODE_WS_RE.search(original_command):
        _log(BASH_SECURITY_CHECK_IDS["UNICODE_WHITESPACE"])
        return {
            "behavior": "ask",
            "message": "Command contains Unicode whitespace characters that could cause parsing inconsistencies",
        }
    return {"behavior": "passthrough", "message": "No Unicode whitespace"}


_MID_WORD_HASH_RE = re.compile(r"\S(?<!\$\{)#")
_CONTINUATION_RE = re.compile(r"\\+\n")


def _join_continuations(text: str) -> str:
    """Collapse escaped newlines while preserving pairs of literal backslashes."""

    def _repl(match: re.Match[str]) -> str:
        backslash_count = len(match.group(0)) - 1
        if backslash_count % 2 == 1:
            return "\\" * (backslash_count - 1)
        return match.group(0)

    return _CONTINUATION_RE.sub(_repl, text)


def validate_mid_word_hash(context: ValidationContext) -> PermissionResult:
    unquoted_keep_quote_chars = context.unquoted_keep_quote_chars
    joined = _join_continuations(unquoted_keep_quote_chars)
    if _MID_WORD_HASH_RE.search(unquoted_keep_quote_chars) or _MID_WORD_HASH_RE.search(
        joined
    ):
        _log(BASH_SECURITY_CHECK_IDS["MID_WORD_HASH"])
        return {
            "behavior": "ask",
            "message": "Command contains mid-word # which is parsed differently by shell-quote vs bash",
        }
    return {"behavior": "passthrough", "message": "No mid-word hash"}


_COMMENT_QUOTE_RE = re.compile(r"['\"]")


def validate_comment_quote_desync(context: ValidationContext) -> PermissionResult:
    if context.tree_sitter:
        return {
            "behavior": "passthrough",
            "message": "Tree-sitter quote context is authoritative",
        }

    original_command = context.original_command

    in_single_quote = False
    in_double_quote = False
    escaped = False

    i = 0
    length = len(original_command)
    while i < length:
        char = original_command[i]

        if escaped:
            escaped = False
            i += 1
            continue

        if in_single_quote:
            if char == "'":
                in_single_quote = False
            i += 1
            continue

        if char == "\\":
            escaped = True
            i += 1
            continue

        if in_double_quote:
            if char == '"':
                in_double_quote = False
            i += 1
            continue

        if char == "'":
            in_single_quote = True
            i += 1
            continue

        if char == '"':
            in_double_quote = True
            i += 1
            continue

        if char == "#":
            line_end = original_command.find("\n", i)
            comment_text = original_command[
                i + 1 : (length if line_end == -1 else line_end)
            ]
            if _COMMENT_QUOTE_RE.search(comment_text):
                _log(BASH_SECURITY_CHECK_IDS["COMMENT_QUOTE_DESYNC"])
                return {
                    "behavior": "ask",
                    "message": "Command contains quote characters inside a # comment which can desync quote tracking",
                }
            if line_end == -1:
                break
            i = line_end  # Loop increment moves past the newline.

        i += 1

    return {"behavior": "passthrough", "message": "No comment quote desync"}


def validate_quoted_newline(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    if "\n" not in original_command or "#" not in original_command:
        return {"behavior": "passthrough", "message": "No newline or no hash"}

    in_single_quote = False
    in_double_quote = False
    escaped = False

    length = len(original_command)
    for i in range(length):
        char = original_command[i]

        if escaped:
            escaped = False
            continue

        if char == "\\" and not in_single_quote:
            escaped = True
            continue

        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue

        if char == "\n" and (in_single_quote or in_double_quote):
            line_start = i + 1
            next_newline = original_command.find("\n", line_start)
            line_end = length if next_newline == -1 else next_newline
            next_line = original_command[line_start:line_end]
            if next_line.strip().startswith("#"):
                _log(BASH_SECURITY_CHECK_IDS["QUOTED_NEWLINE"])
                return {
                    "behavior": "ask",
                    "message": "Command contains a quoted newline followed by a #-prefixed line, which can hide arguments from line-based permission checks",
                }

    return {"behavior": "passthrough", "message": "No quoted newline-hash pattern"}


_ZSH_PRECOMMAND_MODIFIERS = frozenset({"command", "builtin", "noglob", "nocorrect"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")
_WHITESPACE_SPLIT_RE = re.compile(r"\s+")
_FC_E_RE = re.compile(r"\s-\S*e")


def validate_zsh_dangerous_commands(context: ValidationContext) -> PermissionResult:
    original_command = context.original_command

    trimmed = original_command.strip()
    tokens = _WHITESPACE_SPLIT_RE.split(trimmed) if trimmed else [""]
    base_cmd = ""
    for token in tokens:
        if _ENV_ASSIGN_RE.match(token):
            continue
        if token in _ZSH_PRECOMMAND_MODIFIERS:
            continue
        base_cmd = token
        break

    if base_cmd in ZSH_DANGEROUS_COMMANDS:
        _log(BASH_SECURITY_CHECK_IDS["ZSH_DANGEROUS_COMMANDS"], 1)
        return {
            "behavior": "ask",
            "message": f"Command uses Zsh-specific '{base_cmd}' which can bypass security checks",
        }

    if base_cmd == "fc" and _FC_E_RE.search(trimmed):
        _log(BASH_SECURITY_CHECK_IDS["ZSH_DANGEROUS_COMMANDS"], 2)
        return {
            "behavior": "ask",
            "message": "Command uses 'fc -e' which can execute arbitrary commands via editor",
        }

    return {"behavior": "passthrough", "message": "No Zsh dangerous commands"}


# Non-printable control chars (excludes tab/newline/CR, handled elsewhere).
CONTROL_CHAR_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


_EARLY_VALIDATORS = [
    validate_empty,
    validate_incomplete_commands,
    validate_safe_command_substitution,
    validate_git_commit,
]

_NON_MISPARSING_VALIDATORS = frozenset({validate_newlines, validate_redirections})

_VALIDATORS = [
    validate_jq_command,
    validate_obfuscated_flags,
    validate_shell_metacharacters,
    validate_dangerous_variables,
    validate_comment_quote_desync,
    validate_quoted_newline,
    validate_carriage_return,
    validate_newlines,
    validate_ifs_injection,
    validate_proc_environ_access,
    validate_dangerous_patterns,
    validate_redirections,
    validate_backslash_escaped_whitespace,
    validate_backslash_escaped_operators,
    validate_unicode_whitespace,
    validate_mid_word_hash,
    validate_brace_expansion,
    validate_zsh_dangerous_commands,
    validate_malformed_token_injection,
]


def _run_early_validators(context: ValidationContext) -> PermissionResult | None:
    for validator in _EARLY_VALIDATORS:
        result = validator(context)
        if result.get("behavior") == "allow":
            reason = result.get("decisionReason") or {}
            return {
                "behavior": "passthrough",
                "message": (
                    reason["reason"]
                    if isinstance(reason, dict)
                    and reason.get("type") in ("other", "safetyCheck")
                    else "Command allowed"
                ),
            }
        if result.get("behavior") != "passthrough":
            if result.get("behavior") == "ask":
                return {**result, "isBashSecurityCheckForMisparsing": True}
            return result
    return None


def _run_main_validators(context: ValidationContext) -> PermissionResult:
    deferred_non_misparsing_result: PermissionResult | None = None
    for validator in _VALIDATORS:
        result = validator(context)
        if result.get("behavior") == "ask":
            if validator in _NON_MISPARSING_VALIDATORS:
                if deferred_non_misparsing_result is None:
                    deferred_non_misparsing_result = result
                continue
            return {**result, "isBashSecurityCheckForMisparsing": True}
    if deferred_non_misparsing_result is not None:
        return deferred_non_misparsing_result

    return {"behavior": "passthrough", "message": "Command passed all security checks"}


def bash_command_is_safe_deprecated(command: str) -> PermissionResult:
    """The legacy sync security gate.

    Only used when tree-sitter is unavailable. The primary gate is ``parse_for_security``.
    """
    if CONTROL_CHAR_RE.search(command):
        _log(BASH_SECURITY_CHECK_IDS["CONTROL_CHARACTERS"])
        return {
            "behavior": "ask",
            "message": "Command contains non-printable control characters that could be used to bypass security checks",
            "isBashSecurityCheckForMisparsing": True,
        }

    if has_shell_quote_single_quote_bug(command):
        return {
            "behavior": "ask",
            "message": "Command contains single-quoted backslash pattern that could bypass security checks",
            "isBashSecurityCheckForMisparsing": True,
        }

    processed_command = extract_heredocs(command, {"quotedOnly": True}).processed_command

    base_command = command.split(" ")[0] if command.split(" ") else ""
    extracted = extract_quoted_content(processed_command, base_command == "jq")

    context = ValidationContext(
        {
            "original_command": command,
            "base_command": base_command,
            "unquoted_content": extracted["withDoubleQuotes"],
            "fully_unquoted_content": strip_safe_redirections(extracted["fullyUnquoted"]),
            "fully_unquoted_pre_strip": extracted["fullyUnquoted"],
            "unquoted_keep_quote_chars": extracted["unquotedKeepQuoteChars"],
        }
    )

    early = _run_early_validators(context)
    if early is not None:
        return early

    return _run_main_validators(context)


async def bash_command_is_safe_async_deprecated(
    command: str,
    on_divergence: Any = None,
) -> PermissionResult:
    """Run the legacy asynchronous bash safety check.

    Uses tree-sitter quote context when available, falling back to the sync version.
    """
    parsed = await ParsedCommand.parse(command)
    ts_analysis = parsed.get_tree_sitter_analysis() if parsed is not None else None

    if not ts_analysis:
        return bash_command_is_safe_deprecated(command)

    if CONTROL_CHAR_RE.search(command):
        _log(BASH_SECURITY_CHECK_IDS["CONTROL_CHARACTERS"])
        return {
            "behavior": "ask",
            "message": "Command contains non-printable control characters that could be used to bypass security checks",
            "isBashSecurityCheckForMisparsing": True,
        }

    if has_shell_quote_single_quote_bug(command):
        return {
            "behavior": "ask",
            "message": "Command contains single-quoted backslash pattern that could bypass security checks",
            "isBashSecurityCheckForMisparsing": True,
        }

    processed_command = extract_heredocs(command, {"quotedOnly": True}).processed_command

    base_command = command.split(" ")[0] if command.split(" ") else ""

    ts_quote = ts_analysis["quoteContext"]
    regex_quote = extract_quoted_content(processed_command, base_command == "jq")

    with_double_quotes = ts_quote["withDoubleQuotes"]
    fully_unquoted = ts_quote["fullyUnquoted"]
    unquoted_keep_quote_chars = ts_quote["unquotedKeepQuoteChars"]

    context = ValidationContext(
        {
            "original_command": command,
            "base_command": base_command,
            "unquoted_content": with_double_quotes,
            "fully_unquoted_content": strip_safe_redirections(fully_unquoted),
            "fully_unquoted_pre_strip": fully_unquoted,
            "unquoted_keep_quote_chars": unquoted_keep_quote_chars,
            "tree_sitter": ts_analysis,
        }
    )

    if not ts_analysis["dangerousPatterns"]["hasHeredoc"]:
        has_divergence = (
            ts_quote["fullyUnquoted"] != regex_quote["fullyUnquoted"]
            or ts_quote["withDoubleQuotes"] != regex_quote["withDoubleQuotes"]
        )
        if has_divergence:
            if on_divergence:
                on_divergence()
            else:
                pass

    early = _run_early_validators(context)
    if early is not None:
        return early

    return _run_main_validators(context)


__all__ = [
    "BASH_SECURITY_CHECK_IDS",
    "bash_command_is_safe_async_deprecated",
    "bash_command_is_safe_deprecated",
    "has_safe_heredoc_substitution",
    "strip_safe_heredoc_substitutions",
]
