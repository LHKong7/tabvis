"""Bash permission resolver.

This is the headline ``bash_tool_has_permission`` resolver: it parses a bash command,
applies exact/prefix/wildcard allow/deny/ask rules, validates path constraints + sed
constraints + permission mode, runs the security (command-injection) gate, and decides
allow / ask / deny / passthrough for the orchestrator.

Casing: Python identifiers are snake_case; the permission-result / suggestion / rule dicts
round-trip to settings JSON and the transcript, so they keep their wire keys verbatim
(``behavior`` / ``decisionReason`` / ``updatedInput`` / ``suggestions`` / ``ruleValue`` /
``ruleContent`` / ``toolName`` / ``destination`` / ``type`` / ``reason`` / ``rule`` /
``reasons`` / ``message``).

CYCLE-BREAK: ``bash_command_helpers`` / ``mode_validation`` / ``path_validation`` /
``sed_validation`` / ``should_use_sandbox`` and ``tabvis.agent.tools.bash_tool`` all reference this
module (directly or transitively). Those are imported **lazily** (function-local) so this
module import-smokes standalone even if a cyclic sibling is not yet on disk. ``BASH_TOOL_NAME``
is inlined as the literal ``"Bash"`` (matches ``tabvis/tools/bash_tool.py:51``) and the input
schema is referenced via :data:`TYPE_CHECKING` only — never a runtime top-level import of
``tabvis.agent.tools.bash_tool``.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

from tabvis.agent.tools.bash_security import (
    bash_command_is_safe_async_deprecated,
    strip_safe_heredoc_substitutions)
from tabvis.utils.array import count
from tabvis.utils.bash.ast import (
    check_semantics,
    parse_for_security_from_ast)
from tabvis.utils.bash.commands import (
    extract_output_redirections,
    get_command_subcommand_prefix,
    split_command_deprecated)
from tabvis.utils.bash.parser import parse_command_raw
from tabvis.utils.bash.shell_quote import try_parse_shell_command
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.permissions.permission_rule_parser import (
    permission_rule_value_to_string)
from tabvis.utils.permissions.permission_update import extract_rules
from tabvis.utils.permissions.permissions import (
    get_allow_rules,
    get_ask_rules,
    get_deny_rules,
    permission_rule_source_display_string)
from tabvis.utils.permissions.shell_rule_matching import (
    match_wildcard_pattern as shared_match_wildcard_pattern)
from tabvis.utils.permissions.shell_rule_matching import (
    parse_permission_rule)
from tabvis.utils.permissions.shell_rule_matching import (
    permission_rule_extract_prefix as shared_permission_rule_extract_prefix)
from tabvis.utils.permissions.shell_rule_matching import (
    suggestion_for_exact_command as shared_suggestion_for_exact_command)
from tabvis.utils.permissions.shell_rule_matching import (
    suggestion_for_prefix as shared_suggestion_for_prefix)
from tabvis.utils.platform import get_platform
from tabvis.utils.sandbox.sandbox_adapter import SandboxManager
from tabvis.utils.tool_errors import AbortError
from tabvis.utils.windows_paths import windows_path_to_posix_path

if TYPE_CHECKING:  # pragma: no cover - type-only refs (cycle-safe)
    from tabvis.tool import ToolPermissionContext, ToolUseContext
    from tabvis.utils.bash.ast import Redirect, SimpleCommand
    from tabvis.utils.permissions.shell_rule_matching import ShellPermissionRule

# Inlined from src/tools/BashTool/toolName.ts (tabvis/tools/bash_tool.py:51) — importing
# tabvis.agent.tools.bash_tool at top level would create the bash_permissions <-> bash_tool cycle.
BASH_TOOL_NAME = "Bash"


# Env-var assignment prefix (VAR=value). Shared across three while-loops that
# skip safe env vars before extracting the command name.
ENV_VAR_ASSIGN_RE = re.compile(r"^[A-Za-z_]\w*=")

# CC-643: cap subcommand fanout for the legacy splitCommand path; above the cap we
# fall back to 'ask' (safe default — we can't prove safety, so we prompt).
MAX_SUBCOMMANDS_FOR_SECURITY_CHECK = 50

# GH#11380: cap the number of per-subcommand rules suggested for compound commands.
MAX_SUGGESTED_RULES_FOR_COMPOUND = 5


# ---------------------------------------------------------------------------
# Inlined permission helpers corresponding to two functions in
# src/utils/permissions/permissions.ts that are not yet on the shared module
# (createPermissionRequestMessage, getRuleByContentsForToolName). Faithful
# implementations reusing the existing rule extractors / serializers.
# ---------------------------------------------------------------------------


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    """Naive English pluralisation."""
    if n == 1:
        return singular
    return plural if plural is not None else f"{singular}s"


def create_permission_request_message(
    tool_name: str,
    decision_reason: dict[str, Any] | None = None,
) -> str:
    """Create the permission request message."""
    if decision_reason:
        reason_type = decision_reason.get("type")
        if reason_type == "hook":
            if decision_reason.get("reason"):
                return (
                    f"Hook '{decision_reason.get('hookName')}' blocked this action: "
                    f"{decision_reason['reason']}"
                )
            return (
                f"Hook '{decision_reason.get('hookName')}' requires approval for this "
                f"{tool_name} command"
            )
        if reason_type == "rule":
            rule = decision_reason["rule"]
            rule_string = permission_rule_value_to_string(rule["ruleValue"])
            source_string = permission_rule_source_display_string(rule["source"])
            return (
                f"Permission rule '{rule_string}' from {source_string} requires "
                f"approval for this {tool_name} command"
            )
        if reason_type == "subcommandResults":
            needs_approval: list[str] = []
            for cmd, result in decision_reason["reasons"].items():
                if result.get("behavior") in ("ask", "passthrough"):
                    # Strip output redirections for display (Bash only) to avoid
                    # showing filenames as commands.
                    if tool_name == "Bash":
                        extracted = extract_output_redirections(cmd)
                        display_cmd = (
                            extracted["commandWithoutRedirections"]
                            if extracted["redirections"]
                            else cmd
                        )
                        needs_approval.append(display_cmd)
                    else:
                        needs_approval.append(cmd)
            if needs_approval:
                n = len(needs_approval)
                return (
                    f"This {tool_name} command contains multiple operations. The following "
                    f"{_plural(n, 'part')} {_plural(n, 'requires', 'require')} approval: "
                    f"{', '.join(needs_approval)}"
                )
            return (
                f"This {tool_name} command contains multiple operations that require approval"
            )
        if reason_type == "permissionPromptTool":
            return (
                f"Tool '{decision_reason.get('permissionPromptToolName')}' requires approval "
                f"for this {tool_name} command"
            )
        if reason_type == "sandboxOverride":
            return "Run outside of the sandbox"
        if reason_type in ("workingDir", "safetyCheck", "other", "asyncAgent"):
            return decision_reason["reason"]
        if reason_type == "mode":
            # permissionModeTitle is UI-only; fall through to a faithful message shape.
            mode = decision_reason.get("mode")
            return (
                f"Current permission mode ({mode}) requires approval for this "
                f"{tool_name} command"
            )

    return (
        f"Tabvis requested permissions to use {tool_name}, but you haven't granted it yet."
    )


def _get_rule_by_contents_for_tool_name(
    context: ToolPermissionContext,
    tool_name: str,
    behavior: str,
) -> dict[str, dict[str, Any]]:
    """Content-scoped rules for ``tool_name``."""
    if behavior == "allow":
        rules = get_allow_rules(context)
    elif behavior == "deny":
        rules = get_deny_rules(context)
    elif behavior == "ask":
        rules = get_ask_rules(context)
    else:
        rules = []

    rule_by_contents: dict[str, dict[str, Any]] = {}
    for rule in rules:
        rule_value = rule["ruleValue"]
        if (
            rule_value.get("toolName") == tool_name
            and rule_value.get("ruleContent") is not None
            and rule["ruleBehavior"] == behavior
        ):
            rule_by_contents[rule_value["ruleContent"]] = rule
    return rule_by_contents


# ---------------------------------------------------------------------------
# Prefix / suggestion helpers
# ---------------------------------------------------------------------------


def get_simple_command_prefix(command: str) -> str | None:
    """Stable ``command subcommand`` prefix."""
    tokens = [t for t in command.strip().split() if t]
    if len(tokens) == 0:
        return None

    i = 0
    while i < len(tokens) and ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=")[0]
        is_ant_only_safe = False
        if var_name not in SAFE_ENV_VARS and not is_ant_only_safe:
            return None
        i += 1

    remaining = tokens[i:]
    if len(remaining) < 2:
        return None
    subcmd = remaining[1]
    if not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", subcmd):
        return None
    return " ".join(remaining[0:2])


# Bare-prefix suggestions like `bash:*` or `sh:*` would allow arbitrary code via `-c`.
BARE_SHELL_PREFIXES = frozenset(
    {
        "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash", "cmd",
        "powershell", "pwsh",
        # wrappers that exec their args as a command
        "env", "xargs",
        # checkSemantics strips these wrappers to check the wrapped command
        "nice", "stdbuf", "nohup", "timeout", "time",
        # privilege escalation
        "sudo", "doas", "pkexec",
    }
)


def get_first_word_prefix(command: str) -> str | None:
    """UI-only first-word fallback prefix."""
    tokens = [t for t in command.strip().split() if t]

    i = 0
    while i < len(tokens) and ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=")[0]
        is_ant_only_safe = False
        if var_name not in SAFE_ENV_VARS and not is_ant_only_safe:
            return None
        i += 1

    cmd = tokens[i] if i < len(tokens) else None
    if not cmd:
        return None
    if not re.match(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$", cmd):
        return None
    if cmd in BARE_SHELL_PREFIXES:
        return None
    return cmd


def _suggestion_for_exact_command(command: str) -> list[dict]:
    """Build an allow-rule suggestion for an exact command."""
    heredoc_prefix = _extract_prefix_before_heredoc(command)
    if heredoc_prefix:
        return shared_suggestion_for_prefix(BASH_TOOL_NAME, heredoc_prefix)

    if "\n" in command:
        first_line = command.split("\n")[0].strip()
        if first_line:
            return shared_suggestion_for_prefix(BASH_TOOL_NAME, first_line)

    prefix = get_simple_command_prefix(command)
    if prefix:
        return shared_suggestion_for_prefix(BASH_TOOL_NAME, prefix)

    return shared_suggestion_for_exact_command(BASH_TOOL_NAME, command)


def _extract_prefix_before_heredoc(command: str) -> str | None:
    """Stable prefix before a heredoc operator."""
    if "<<" not in command:
        return None

    idx = command.index("<<")
    if idx <= 0:
        return None

    before = command[:idx].strip()
    if not before:
        return None

    prefix = get_simple_command_prefix(before)
    if prefix:
        return prefix

    tokens = [t for t in before.split() if t]
    i = 0
    while i < len(tokens) and ENV_VAR_ASSIGN_RE.match(tokens[i]):
        var_name = tokens[i].split("=")[0]
        is_ant_only_safe = False
        if var_name not in SAFE_ENV_VARS and not is_ant_only_safe:
            return None
        i += 1
    if i >= len(tokens):
        return None
    return " ".join(tokens[i : i + 2]) or None


def suggestion_for_prefix(prefix: str) -> list[dict]:
    """Build an allow-rule suggestion for a command prefix."""
    return shared_suggestion_for_prefix(BASH_TOOL_NAME, prefix)


# Delegates to the shared implementations.
permission_rule_extract_prefix = shared_permission_rule_extract_prefix


def match_wildcard_pattern(pattern: str, command: str) -> bool:
    """Match a command against a wildcard pattern (case-sensitive for Bash)."""
    return shared_match_wildcard_pattern(pattern, command)


def bash_permission_rule(permission_rule: str) -> ShellPermissionRule:
    """Parse a permission rule into a structured rule object (delegates to shared)."""
    return parse_permission_rule(permission_rule)


# ---------------------------------------------------------------------------
# Safe-env-var whitelists
# ---------------------------------------------------------------------------

SAFE_ENV_VARS = frozenset(
    {
        # Go
        "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GO111MODULE",
        # Rust
        "RUST_BACKTRACE", "RUST_LOG",
        # Node
        "NODE_ENV",
        # Python
        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
        # Pytest
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG",
        # API keys
        "TABVIS_API_KEY",
        # Locale
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_TIME", "CHARSET",
        # Terminal / display
        "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "TZ",
        # Color config
        "LS_COLORS", "LSCOLORS", "GREP_COLOR", "GREP_COLORS", "GCC_COLORS",
        # Display formatting
        "TIME_STYLE", "BLOCK_SIZE", "BLOCKSIZE",
    }
)

ANT_ONLY_SAFE_ENV_VARS = frozenset(
    {
        "KUBECONFIG", "DOCKER_HOST", "CLUSTER",
        "COO_CLUSTER", "COO_CLUSTER_NAME", "COO_NAMESPACE", "COO_LAUNCH_YAML_DRY_RUN",
        "SKIP_NODE_VERSION_CHECK", "EXPECTTEST_ACCEPT", "CI", "GIT_LFS_SKIP_SMUDGE",
        "CUDA_VISIBLE_DEVICES", "JAX_PLATFORMS",
        "COLUMNS", "TMUX",
        "POSTGRESQL_VERSION", "FIRESTORE_EMULATOR_HOST", "HARNESS_QUIET",
        "TEST_CROSSCHECK_LISTS_MATCH_UPDATE", "DBT_PER_DEVELOPER_ENVIRONMENTS",
        "STATSIG_FORD_DB_CHECKS",
        "ANT_ENVIRONMENT", "ANT_SERVICE", "MONOREPO_ROOT_DIR",
        "PYENV_VERSION",
        "PGPASSWORD", "GH_TOKEN", "GROWTHBOOK_API_KEY",
    }
)


def _strip_comment_lines(command: str) -> str:
    """Drop full-line comments (whole-line ``#``)."""
    lines = command.split("\n")
    non_comment_lines = [
        line
        for line in lines
        if line.strip() != "" and not line.strip().startswith("#")
    ]
    if len(non_comment_lines) == 0:
        return command
    return "\n".join(non_comment_lines)


# Safe wrapper patterns (timeout, time, nice, stdbuf, nohup). KEEP IN SYNC with
# stripWrappersFromArgv / checkSemantics.
_SAFE_WRAPPER_PATTERNS = [
    re.compile(
        r"^timeout[ \t]+"
        r"(?:(?:--(?:foreground|preserve-status|verbose)"
        r"|--(?:kill-after|signal)=[A-Za-z0-9_.+-]+"
        r"|--(?:kill-after|signal)[ \t]+[A-Za-z0-9_.+-]+"
        r"|-v|-[ks][ \t]+[A-Za-z0-9_.+-]+|-[ks][A-Za-z0-9_.+-]+)[ \t]+)*"
        r"(?:--[ \t]+)?\d+(?:\.\d+)?[smhd]?[ \t]+"
    ),
    re.compile(r"^time[ \t]+(?:--[ \t]+)?"),
    re.compile(r"^nice(?:[ \t]+-n[ \t]+-?\d+|[ \t]+-\d+)?[ \t]+(?:--[ \t]+)?"),
    re.compile(r"^stdbuf(?:[ \t]+-[ioe][LN0-9]+)+[ \t]+(?:--[ \t]+)?"),
    re.compile(r"^nohup[ \t]+(?:--[ \t]+)?"),
]

# Pattern for env var assignments (unquoted, safe characters only).
_ENV_VAR_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_./:-]+)[ \t]+")


def strip_safe_wrappers(command: str) -> str:
    """Strip safe env vars + wrapper commands."""
    stripped = command
    previous_stripped = ""

    # Phase 1: strip leading env vars and comments only.
    while stripped != previous_stripped:
        previous_stripped = stripped
        stripped = _strip_comment_lines(stripped)

        env_var_match = _ENV_VAR_PATTERN.match(stripped)
        if env_var_match:
            var_name = env_var_match.group(1)
            is_ant_only_safe = False
            if var_name in SAFE_ENV_VARS or is_ant_only_safe:
                stripped = _ENV_VAR_PATTERN.sub("", stripped, count=1)

    # Phase 2: strip wrapper commands and comments only. Do NOT strip env vars.
    previous_stripped = ""
    while stripped != previous_stripped:
        previous_stripped = stripped
        stripped = _strip_comment_lines(stripped)
        for pattern in _SAFE_WRAPPER_PATTERNS:
            stripped = pattern.sub("", stripped, count=1)

    return stripped.strip()


# SECURITY: allowlist for timeout flag VALUES.
_TIMEOUT_FLAG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_TIMEOUT_LONG_KV_RE = re.compile(r"^--(?:kill-after|signal)=[A-Za-z0-9_.+-]+$")
_TIMEOUT_SHORT_FUSED_RE = re.compile(r"^-[ks][A-Za-z0-9_.+-]+$")
_TIMEOUT_DURATION_RE = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_NICE_N_VALUE_RE = re.compile(r"^-?\d+$")


def _skip_timeout_flags(a: list[str]) -> int:
    """Index of the DURATION token, or -1 if unparseable."""
    i = 1
    while i < len(a):
        arg = a[i]
        nxt = a[i + 1] if i + 1 < len(a) else None
        if arg in ("--foreground", "--preserve-status", "--verbose"):
            i += 1
        elif _TIMEOUT_LONG_KV_RE.match(arg):
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


def strip_wrappers_from_argv(argv: list[str]) -> list[str]:
    """Argv-level wrapper stripping."""
    a = argv
    while True:
        if a and a[0] in ("time", "nohup"):
            a = a[2:] if len(a) > 1 and a[1] == "--" else a[1:]
        elif a and a[0] == "timeout":
            i = _skip_timeout_flags(a)
            if i < 0 or i >= len(a) or not _TIMEOUT_DURATION_RE.match(a[i]):
                return a
            a = a[i + 1 :]
        elif (
            len(a) >= 3
            and a[0] == "nice"
            and a[1] == "-n"
            and a[2]
            and _NICE_N_VALUE_RE.match(a[2])
        ):
            a = a[4:] if len(a) > 3 and a[3] == "--" else a[3:]
        else:
            return a


# Env vars that make a *different binary* run (injection or resolution hijack).
BINARY_HIJACK_VARS = re.compile(r"^(LD_|DYLD_|PATH$)")

# Broader value pattern for deny-rule stripping (HackerOne hardening).
_ALL_LEADING_ENV_VAR_PATTERN = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?)\+?="
    r"(?:'[^'\n\r]*'|\"(?:\\.|[^\"$`\\\n\r])*\"|\\.|[^ \t\n\r$`;|&()<>\\'\"])*"
    r"[ \t]+"
)


def strip_all_leading_env_vars(
    command: str,
    blocklist: re.Pattern[str] | None = None,
) -> str:
    """Strip ALL leading env var prefixes."""
    stripped = command
    previous_stripped = ""

    while stripped != previous_stripped:
        previous_stripped = stripped
        stripped = _strip_comment_lines(stripped)

        m = _ALL_LEADING_ENV_VAR_PATTERN.match(stripped)
        if not m:
            continue
        if blocklist is not None and blocklist.search(m.group(1)):
            break
        stripped = stripped[len(m.group(0)) :]

    return stripped.strip()


# ---------------------------------------------------------------------------
# Rule filtering / matching
# ---------------------------------------------------------------------------


def _filter_rules_by_contents_matching_input(
    input: Any,
    rules: dict[str, dict[str, Any]],
    match_mode: str,
    *,
    strip_all_env_vars: bool = False,
    skip_compound_check: bool = False,
) -> list[dict[str, Any]]:
    """Filter permission rules whose contents match the command input."""
    command = _input_command(input).strip()

    command_without_redirections = extract_output_redirections(command)[
        "commandWithoutRedirections"
    ]

    commands_for_matching = (
        [command, command_without_redirections]
        if match_mode == "exact"
        else [command_without_redirections]
    )

    # Strip safe wrappers + env vars.
    commands_to_try: list[str] = []
    for cmd in commands_for_matching:
        stripped_command = strip_safe_wrappers(cmd)
        if stripped_command != cmd:
            commands_to_try.extend([cmd, stripped_command])
        else:
            commands_to_try.append(cmd)

    if strip_all_env_vars:
        seen = set(commands_to_try)
        start_idx = 0
        while start_idx < len(commands_to_try):
            end_idx = len(commands_to_try)
            for i in range(start_idx, end_idx):
                cmd = commands_to_try[i]
                if not cmd:
                    continue
                env_stripped = strip_all_leading_env_vars(cmd)
                if env_stripped not in seen:
                    commands_to_try.append(env_stripped)
                    seen.add(env_stripped)
                wrapper_stripped = strip_safe_wrappers(cmd)
                if wrapper_stripped not in seen:
                    commands_to_try.append(wrapper_stripped)
                    seen.add(wrapper_stripped)
            start_idx = end_idx

    is_compound_command: dict[str, bool] = {}
    if match_mode == "prefix" and not skip_compound_check:
        for cmd in commands_to_try:
            if cmd not in is_compound_command:
                is_compound_command[cmd] = len(split_command_deprecated(cmd)) > 1

    matched: list[dict[str, Any]] = []
    for rule_content, rule in rules.items():
        bash_rule = bash_permission_rule(rule_content)
        if any(
            _command_matches_rule(
                bash_rule, cmd_to_match, match_mode, is_compound_command
            )
            for cmd_to_match in commands_to_try
        ):
            matched.append(rule)
    return matched


def _command_matches_rule(
    bash_rule: dict[str, Any],
    cmd_to_match: str,
    match_mode: str,
    is_compound_command: dict[str, bool],
) -> bool:
    """Single-rule match dispatch (extracted from the TS ``.some`` callback)."""
    rule_type = bash_rule["type"]
    if rule_type == "exact":
        return bash_rule["command"] == cmd_to_match
    if rule_type == "prefix":
        if match_mode == "exact":
            return bash_rule["prefix"] == cmd_to_match
        # prefix mode
        if is_compound_command.get(cmd_to_match):
            return False
        prefix = bash_rule["prefix"]
        if cmd_to_match == prefix:
            return True
        if cmd_to_match.startswith(prefix + " "):
            return True
        xargs_prefix = "xargs " + prefix
        if cmd_to_match == xargs_prefix:
            return True
        return cmd_to_match.startswith(xargs_prefix + " ")
    if rule_type == "wildcard":
        if match_mode == "exact":
            return False
        if is_compound_command.get(cmd_to_match):
            return False
        return match_wildcard_pattern(bash_rule["pattern"], cmd_to_match)
    return False


def _matching_rules_for_input(
    input: Any,
    tool_permission_context: ToolPermissionContext,
    match_mode: str,
    *,
    skip_compound_check: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Deny/ask/allow rule matches."""
    deny_rule_by_contents = _get_rule_by_contents_for_tool_name(
        tool_permission_context, BASH_TOOL_NAME, "deny"
    )
    matching_deny_rules = _filter_rules_by_contents_matching_input(
        input,
        deny_rule_by_contents,
        match_mode,
        strip_all_env_vars=True,
        skip_compound_check=True,
    )

    ask_rule_by_contents = _get_rule_by_contents_for_tool_name(
        tool_permission_context, BASH_TOOL_NAME, "ask"
    )
    matching_ask_rules = _filter_rules_by_contents_matching_input(
        input,
        ask_rule_by_contents,
        match_mode,
        strip_all_env_vars=True,
        skip_compound_check=True,
    )

    allow_rule_by_contents = _get_rule_by_contents_for_tool_name(
        tool_permission_context, BASH_TOOL_NAME, "allow"
    )
    matching_allow_rules = _filter_rules_by_contents_matching_input(
        input,
        allow_rule_by_contents,
        match_mode,
        skip_compound_check=skip_compound_check,
    )

    return {
        "matchingDenyRules": matching_deny_rules,
        "matchingAskRules": matching_ask_rules,
        "matchingAllowRules": matching_allow_rules,
    }


def bash_tool_check_exact_match_permission(
    input: Any,
    tool_permission_context: ToolPermissionContext,
) -> dict[str, Any]:
    """Exact-match deny/ask/allow."""
    command = _input_command(input).strip()
    matches = _matching_rules_for_input(input, tool_permission_context, "exact")
    deny = matches["matchingDenyRules"]
    ask = matches["matchingAskRules"]
    allow = matches["matchingAllowRules"]

    if deny:
        return {
            "behavior": "deny",
            "message": (
                f"Permission to use {BASH_TOOL_NAME} with command {command} has been denied."
            ),
            "decisionReason": {"type": "rule", "rule": deny[0]},
        }

    if ask:
        return {
            "behavior": "ask",
            "message": create_permission_request_message(BASH_TOOL_NAME),
            "decisionReason": {"type": "rule", "rule": ask[0]},
        }

    if allow:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "rule", "rule": allow[0]},
        }

    decision_reason = {"type": "other", "reason": "This command requires approval"}
    return {
        "behavior": "passthrough",
        "message": create_permission_request_message(BASH_TOOL_NAME, decision_reason),
        "decisionReason": decision_reason,
        "suggestions": _suggestion_for_exact_command(command),
    }


def bash_tool_check_permission(
    input: Any,
    tool_permission_context: ToolPermissionContext,
    compound_command_has_cd: bool | None = None,
    ast_command: SimpleCommand | None = None,
) -> dict[str, Any]:
    """Single-subcommand permission decision."""
    # Lazy cyclic imports.
    from tabvis.agent.tools.bash_tool import bash_tool
    from tabvis.agent.tools.mode_validation import check_permission_mode
    from tabvis.agent.tools.path_validation import check_path_constraints
    from tabvis.agent.tools.sed_validation import check_sed_constraints

    command = _input_command(input).strip()

    # 1. Exact match first.
    exact_match_result = bash_tool_check_exact_match_permission(
        input, tool_permission_context
    )
    if exact_match_result["behavior"] in ("deny", "ask"):
        return exact_match_result

    # 2. Prefix/exact rule matches.
    matches = _matching_rules_for_input(
        input,
        tool_permission_context,
        "prefix",
        skip_compound_check=ast_command is not None,
    )
    deny = matches["matchingDenyRules"]
    ask = matches["matchingAskRules"]
    allow = matches["matchingAllowRules"]

    if deny:
        return {
            "behavior": "deny",
            "message": (
                f"Permission to use {BASH_TOOL_NAME} with command {command} has been denied."
            ),
            "decisionReason": {"type": "rule", "rule": deny[0]},
        }

    if ask:
        return {
            "behavior": "ask",
            "message": create_permission_request_message(BASH_TOOL_NAME),
            "decisionReason": {"type": "rule", "rule": ask[0]},
        }

    # 3. Path constraints.
    path_result = check_path_constraints(
        input,
        get_cwd(),
        tool_permission_context,
        compound_command_has_cd,
        ast_command.redirects if ast_command is not None else None,
        [ast_command] if ast_command is not None else None,
    )
    if path_result["behavior"] != "passthrough":
        return path_result

    # 4. Allow if exact match allow.
    if exact_match_result["behavior"] == "allow":
        return exact_match_result

    # 5. Allow if a prefix allow rule matched.
    if allow:
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "rule", "rule": allow[0]},
        }

    # 5b. Sed constraints.
    sed_constraint_result = check_sed_constraints(input, tool_permission_context)
    if sed_constraint_result["behavior"] != "passthrough":
        return sed_constraint_result

    # 6. Mode-specific handling.
    mode_result = check_permission_mode(input, tool_permission_context)
    if mode_result["behavior"] != "passthrough":
        return mode_result

    # 7. Read-only rules.
    if bash_tool.is_read_only(input):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {"type": "other", "reason": "Read-only command is allowed"},
        }

    # 8. Passthrough.
    decision_reason = {"type": "other", "reason": "This command requires approval"}
    return {
        "behavior": "passthrough",
        "message": create_permission_request_message(BASH_TOOL_NAME, decision_reason),
        "decisionReason": decision_reason,
        "suggestions": _suggestion_for_exact_command(command),
    }


async def check_command_and_suggest_rules(
    input: Any,
    tool_permission_context: ToolPermissionContext,
    command_prefix_result: dict[str, Any] | None,
    compound_command_has_cd: bool | None = None,
    ast_parse_succeeded: bool | None = None,
) -> dict[str, Any]:
    """Per-subcommand prefix + suggestions."""
    exact_match_result = bash_tool_check_exact_match_permission(
        input, tool_permission_context
    )
    if exact_match_result["behavior"] != "passthrough":
        return exact_match_result

    permission_result = bash_tool_check_permission(
        input, tool_permission_context, compound_command_has_cd
    )
    if permission_result["behavior"] in ("deny", "ask"):
        return permission_result

    # 3. Command-injection security gate (skip when AST already validated).
    if not ast_parse_succeeded and not is_env_truthy(
        os.environ.get("TABVIS_DISABLE_COMMAND_INJECTION_CHECK")
    ):
        safety_result = await bash_command_is_safe_async_deprecated(
            _input_command(input)
        )
        if safety_result["behavior"] != "passthrough":
            reason = (
                safety_result["message"]
                if safety_result["behavior"] == "ask" and safety_result.get("message")
                else (
                    "This command contains patterns that could pose security risks "
                    "and requires approval"
                )
            )
            decision_reason = {"type": "other", "reason": reason}
            return {
                "behavior": "ask",
                "message": create_permission_request_message(
                    BASH_TOOL_NAME, decision_reason
                ),
                "decisionReason": decision_reason,
                "suggestions": [],
            }

    if permission_result["behavior"] == "allow":
        return permission_result

    suggested_updates = (
        suggestion_for_prefix(command_prefix_result["commandPrefix"])
        if command_prefix_result and command_prefix_result.get("commandPrefix")
        else _suggestion_for_exact_command(_input_command(input))
    )

    return {**permission_result, "suggestions": suggested_updates}


def _check_sandbox_auto_allow(
    input: Any,
    tool_permission_context: ToolPermissionContext,
) -> dict[str, Any]:
    """Sandbox auto-allow respecting deny/ask rules."""
    command = _input_command(input).strip()

    matches = _matching_rules_for_input(input, tool_permission_context, "prefix")
    deny = matches["matchingDenyRules"]
    ask = matches["matchingAskRules"]

    if deny:
        return {
            "behavior": "deny",
            "message": (
                f"Permission to use {BASH_TOOL_NAME} with command {command} has been denied."
            ),
            "decisionReason": {"type": "rule", "rule": deny[0]},
        }

    subcommands = split_command_deprecated(command)
    if len(subcommands) > 1:
        first_ask_rule: dict[str, Any] | None = None
        for sub in subcommands:
            sub_result = _matching_rules_for_input(
                {"command": sub}, tool_permission_context, "prefix"
            )
            if sub_result["matchingDenyRules"]:
                return {
                    "behavior": "deny",
                    "message": (
                        f"Permission to use {BASH_TOOL_NAME} with command {command} "
                        "has been denied."
                    ),
                    "decisionReason": {
                        "type": "rule",
                        "rule": sub_result["matchingDenyRules"][0],
                    },
                }
            if first_ask_rule is None and sub_result["matchingAskRules"]:
                first_ask_rule = sub_result["matchingAskRules"][0]
        if first_ask_rule:
            return {
                "behavior": "ask",
                "message": create_permission_request_message(BASH_TOOL_NAME),
                "decisionReason": {"type": "rule", "rule": first_ask_rule},
            }

    if ask:
        return {
            "behavior": "ask",
            "message": create_permission_request_message(BASH_TOOL_NAME),
            "decisionReason": {"type": "rule", "rule": ask[0]},
        }

    return {
        "behavior": "allow",
        "updatedInput": input,
        "decisionReason": {
            "type": "other",
            "reason": "Auto-allowed with sandbox (autoAllowBashIfSandboxed enabled)",
        },
    }


def _filter_cd_cwd_subcommands(
    source_subcommands: list[str],
    ast_commands: list[SimpleCommand] | None,
    cwd: str,
    cwd_mingw: str,
) -> dict[str, list[Any]]:
    """Drop ``cd ${cwd}`` prefixes, keep AST aligned."""
    subcommands: list[str] = []
    ast_commands_by_idx: list[Any] = []
    for i in range(len(source_subcommands)):
        cmd = source_subcommands[i]
        if cmd in (f"cd {cwd}", f"cd {cwd_mingw}"):
            continue
        subcommands.append(cmd)
        ast_commands_by_idx.append(ast_commands[i] if ast_commands and i < len(ast_commands) else None)
    return {"subcommands": subcommands, "astCommandsByIdx": ast_commands_by_idx}


def _check_early_exit_deny(
    input: Any,
    tool_permission_context: ToolPermissionContext,
) -> dict[str, Any] | None:
    """Exact-match result + prefix/wildcard deny."""
    exact_match_result = bash_tool_check_exact_match_permission(
        input, tool_permission_context
    )
    if exact_match_result["behavior"] != "passthrough":
        return exact_match_result
    deny = _matching_rules_for_input(input, tool_permission_context, "prefix")[
        "matchingDenyRules"
    ]
    if deny:
        return {
            "behavior": "deny",
            "message": (
                f"Permission to use {BASH_TOOL_NAME} with command "
                f"{_input_command(input)} has been denied."
            ),
            "decisionReason": {"type": "rule", "rule": deny[0]},
        }
    return None


def _check_semantics_deny(
    input: Any,
    tool_permission_context: ToolPermissionContext,
    commands: list[Any],
) -> dict[str, Any] | None:
    """Early-exit deny + per-SimpleCommand deny."""
    full_cmd = _check_early_exit_deny(input, tool_permission_context)
    if full_cmd is not None:
        return full_cmd
    for cmd in commands:
        sub_deny = _matching_rules_for_input(
            {**_input_dict(input), "command": cmd.text},
            tool_permission_context,
            "prefix",
        )["matchingDenyRules"]
        if sub_deny:
            return {
                "behavior": "deny",
                "message": (
                    f"Permission to use {BASH_TOOL_NAME} with command "
                    f"{_input_command(input)} has been denied."
                ),
                "decisionReason": {"type": "rule", "rule": sub_deny[0]},
            }
    return None


# ---------------------------------------------------------------------------
# The main resolver
# ---------------------------------------------------------------------------


async def bash_tool_has_permission(
    input: Any,
    context: ToolUseContext,
    get_command_subcommand_prefix_fn: Any = get_command_subcommand_prefix,
) -> dict[str, Any]:
    """The main BashTool permission resolver."""
    from tabvis.agent.tools.bash_command_helpers import (
        CommandIdentityCheckers,
        check_command_operator_permissions)
    from tabvis.agent.tools.path_validation import check_path_constraints
    from tabvis.agent.tools.should_use_sandbox import should_use_sandbox

    app_state = context.get_app_state()

    # 0. AST-based security parse. parse_command_raw is gated-off (returns None),
    # so astRoot is None → parse-unavailable → legacy shell-quote path.
    injection_check_disabled = is_env_truthy(
        os.environ.get("TABVIS_DISABLE_COMMAND_INJECTION_CHECK")
    )
    ast_root = (
        None
        if injection_check_disabled
        else await parse_command_raw(_input_command(input))
    )
    ast_result: dict[str, Any] = (
        parse_for_security_from_ast(_input_command(input), ast_root)
        if ast_root
        else {"kind": "parse-unavailable"}
    )
    ast_subcommands: list[str] | None = None
    ast_redirects: list[Redirect] | None = None
    ast_commands: list[SimpleCommand] | None = None
    shadow_legacy_subs: list[str] | None = None

    if ast_result["kind"] == "too-complex":
        early_exit = _check_early_exit_deny(
            input, app_state["toolPermissionContext"]
        )
        if early_exit is not None:
            return early_exit
        decision_reason = {"type": "other", "reason": ast_result["reason"]}
        return {
            "behavior": "ask",
            "decisionReason": decision_reason,
            "message": create_permission_request_message(
                BASH_TOOL_NAME, decision_reason
            ),
            "suggestions": [],
        }

    if ast_result["kind"] == "simple":
        sem = check_semantics(ast_result["commands"])
        if not sem["ok"]:
            early_exit = _check_semantics_deny(
                input, app_state["toolPermissionContext"], ast_result["commands"]
            )
            if early_exit is not None:
                return early_exit
            decision_reason = {"type": "other", "reason": sem["reason"]}
            return {
                "behavior": "ask",
                "decisionReason": decision_reason,
                "message": create_permission_request_message(
                    BASH_TOOL_NAME, decision_reason
                ),
                "suggestions": [],
            }
        ast_subcommands = [c.text for c in ast_result["commands"]]
        ast_redirects = [r for c in ast_result["commands"] for r in c.redirects]
        ast_commands = ast_result["commands"]

    # Legacy shell-quote pre-check (parse-unavailable path).
    if ast_result["kind"] == "parse-unavailable":
        log_for_debugging(
            "bashToolHasPermission: tree-sitter unavailable, using legacy shell-quote path"
        )
        parse_result = try_parse_shell_command(_input_command(input))
        if not parse_result["success"]:
            decision_reason = {
                "type": "other",
                "reason": (
                    "Command contains malformed syntax that cannot be parsed: "
                    f"{parse_result.get('error')}"
                ),
            }
            return {
                "behavior": "ask",
                "decisionReason": decision_reason,
                "message": create_permission_request_message(
                    BASH_TOOL_NAME, decision_reason
                ),
            }

    # Sandbox auto-allow.
    if (
        SandboxManager.is_sandboxing_enabled()
        and SandboxManager.is_auto_allow_bash_if_sandboxed_enabled()
        and should_use_sandbox(input)
    ):
        sandbox_auto_allow_result = _check_sandbox_auto_allow(
            input, app_state["toolPermissionContext"]
        )
        if sandbox_auto_allow_result["behavior"] != "passthrough":
            return sandbox_auto_allow_result

    # Exact match first.
    exact_match_result = bash_tool_check_exact_match_permission(
        input, app_state["toolPermissionContext"]
    )
    if exact_match_result["behavior"] == "deny":
        return exact_match_result

    # Operator permissions (pipes, redirects, etc.).
    async def _recurse(i: Any) -> dict[str, Any]:
        return await bash_tool_has_permission(i, context, get_command_subcommand_prefix_fn)

    command_operator_result = await check_command_operator_permissions(
        input,
        _recurse,
        CommandIdentityCheckers(
            is_normalized_cd_command=is_normalized_cd_command,
            is_normalized_git_command=is_normalized_git_command,
        ),
        ast_root,
    )
    if command_operator_result["behavior"] != "passthrough":
        if command_operator_result["behavior"] == "allow":
            safety_result = (
                await bash_command_is_safe_async_deprecated(_input_command(input))
                if ast_subcommands is None
                else None
            )
            if (
                safety_result is not None
                and safety_result["behavior"] != "passthrough"
                and safety_result["behavior"] != "allow"
            ):
                app_state = context.get_app_state()
                reason = (
                    safety_result.get("message")
                    or "Command contains patterns that require approval"
                )
                return {
                    "behavior": "ask",
                    "message": create_permission_request_message(
                        BASH_TOOL_NAME, {"type": "other", "reason": reason}
                    ),
                    "decisionReason": {"type": "other", "reason": reason},
                }

            app_state = context.get_app_state()
            path_result = check_path_constraints(
                input,
                get_cwd(),
                app_state["toolPermissionContext"],
                command_has_any_cd(_input_command(input)),
                ast_redirects,
                ast_commands,
            )
            if path_result["behavior"] != "passthrough":
                return path_result

        if command_operator_result["behavior"] == "ask":
            app_state = context.get_app_state()
            return {**command_operator_result}

        return command_operator_result

    # Legacy misparsing gate (only when AST unavailable).
    if ast_subcommands is None and not is_env_truthy(
        os.environ.get("TABVIS_DISABLE_COMMAND_INJECTION_CHECK")
    ):
        original_command_safety_result = await bash_command_is_safe_async_deprecated(
            _input_command(input)
        )
        if original_command_safety_result[
            "behavior"
        ] == "ask" and original_command_safety_result.get(
            "isBashSecurityCheckForMisparsing"
        ):
            remainder = strip_safe_heredoc_substitutions(_input_command(input))
            remainder_result = (
                await bash_command_is_safe_async_deprecated(remainder)
                if remainder is not None
                else None
            )
            if remainder is None or (
                remainder_result is not None
                and remainder_result["behavior"] == "ask"
                and remainder_result.get("isBashSecurityCheckForMisparsing")
            ):
                app_state = context.get_app_state()
                exact_match_result = bash_tool_check_exact_match_permission(
                    input, app_state["toolPermissionContext"]
                )
                if exact_match_result["behavior"] == "allow":
                    return exact_match_result
                decision_reason = {
                    "type": "other",
                    "reason": original_command_safety_result.get("message"),
                }
                return {
                    "behavior": "ask",
                    "message": create_permission_request_message(
                        BASH_TOOL_NAME, decision_reason
                    ),
                    "decisionReason": decision_reason,
                    "suggestions": [],
                }

    # Split into subcommands.
    cwd = get_cwd()
    cwd_mingw = windows_path_to_posix_path(cwd) if get_platform() == "windows" else cwd
    source_subcommands = (
        ast_subcommands
        if ast_subcommands is not None
        else (
            shadow_legacy_subs
            if shadow_legacy_subs is not None
            else split_command_deprecated(_input_command(input))
        )
    )
    filtered = _filter_cd_cwd_subcommands(source_subcommands, ast_commands, cwd, cwd_mingw)
    subcommands = filtered["subcommands"]
    ast_commands_by_idx = filtered["astCommandsByIdx"]

    # CC-643: cap subcommand fanout (legacy path only).
    if (
        ast_subcommands is None
        and len(subcommands) > MAX_SUBCOMMANDS_FOR_SECURITY_CHECK
    ):
        log_for_debugging(
            f"bashPermissions: {len(subcommands)} subcommands exceeds cap "
            f"({MAX_SUBCOMMANDS_FOR_SECURITY_CHECK}) — returning ask",
            {"level": "debug"},
        )
        decision_reason = {
            "type": "other",
            "reason": (
                f"Command splits into {len(subcommands)} subcommands, too many to "
                "safety-check individually"
            ),
        }
        return {
            "behavior": "ask",
            "message": create_permission_request_message(
                BASH_TOOL_NAME, decision_reason
            ),
            "decisionReason": decision_reason,
        }

    # Ask if there are multiple `cd` commands.
    cd_commands = [s for s in subcommands if is_normalized_cd_command(s)]
    if len(cd_commands) > 1:
        decision_reason = {
            "type": "other",
            "reason": (
                "Multiple directory changes in one command require approval for clarity"
            ),
        }
        return {
            "behavior": "ask",
            "decisionReason": decision_reason,
            "message": create_permission_request_message(
                BASH_TOOL_NAME, decision_reason
            ),
        }

    compound_command_has_cd = len(cd_commands) > 0

    # Block compound commands that have both cd AND git.
    if compound_command_has_cd:
        has_git_command = any(
            is_normalized_git_command(cmd.strip()) for cmd in subcommands
        )
        if has_git_command:
            decision_reason = {
                "type": "other",
                "reason": (
                    "Compound commands with cd and git require approval to prevent "
                    "bare repository attacks"
                ),
            }
            return {
                "behavior": "ask",
                "decisionReason": decision_reason,
                "message": create_permission_request_message(
                    BASH_TOOL_NAME, decision_reason
                ),
            }

    app_state = context.get_app_state()

    # Per-subcommand permission decisions.
    subcommand_permission_decisions = [
        bash_tool_check_permission(
            {"command": command},
            app_state["toolPermissionContext"],
            compound_command_has_cd,
            ast_commands_by_idx[i],
        )
        for i, command in enumerate(subcommands)
    ]

    # Deny if any subcommand is denied.
    denied_subresult = next(
        (r for r in subcommand_permission_decisions if r["behavior"] == "deny"), None
    )
    if denied_subresult is not None:
        return {
            "behavior": "deny",
            "message": (
                f"Permission to use {BASH_TOOL_NAME} with command {_input_command(input)} "
                "has been denied."
            ),
            "decisionReason": {
                "type": "subcommandResults",
                "reasons": {
                    subcommands[i]: result
                    for i, result in enumerate(subcommand_permission_decisions)
                },
            },
        }

    # Validate output redirections on the ORIGINAL command.
    path_result = check_path_constraints(
        input,
        get_cwd(),
        app_state["toolPermissionContext"],
        compound_command_has_cd,
        ast_redirects,
        ast_commands,
    )
    if path_result["behavior"] == "deny":
        return path_result

    ask_subresult = next(
        (r for r in subcommand_permission_decisions if r["behavior"] == "ask"), None
    )
    non_allow_count = count(
        subcommand_permission_decisions, lambda r: r["behavior"] != "allow"
    )

    if path_result["behavior"] == "ask" and ask_subresult is None:
        return path_result

    if ask_subresult is not None and non_allow_count == 1:
        return {**ask_subresult}

    if exact_match_result["behavior"] == "allow":
        return exact_match_result

    # If all subcommands allowed via exact/prefix match + no injection.
    has_possible_command_injection = False
    if ast_subcommands is None and not is_env_truthy(
        os.environ.get("TABVIS_DISABLE_COMMAND_INJECTION_CHECK")
    ):
        divergence_count = 0

        def _on_divergence() -> None:
            nonlocal divergence_count
            divergence_count += 1

        results = [
            await bash_command_is_safe_async_deprecated(c, _on_divergence)
            for c in subcommands
        ]
        has_possible_command_injection = any(
            r["behavior"] != "passthrough" for r in results
        )
        if divergence_count > 0:
            pass
    if (
        all(r["behavior"] == "allow" for r in subcommand_permission_decisions)
        and not has_possible_command_injection
    ):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "subcommandResults",
                "reasons": {
                    subcommands[i]: result
                    for i, result in enumerate(subcommand_permission_decisions)
                },
            },
        }

    # Query Haiku for command prefixes (skipped unless a custom fn is injected).
    command_subcommand_prefix: Any = None
    if get_command_subcommand_prefix_fn is not get_command_subcommand_prefix:
        command_subcommand_prefix = await get_command_subcommand_prefix_fn(
            _input_command(input),
            context.abort_controller.signal,
            context.options.is_non_interactive_session,
        )
        if context.abort_controller.signal.aborted:
            raise AbortError()

    app_state = context.get_app_state()
    if len(subcommands) == 1:
        result = await check_command_and_suggest_rules(
            {"command": subcommands[0]},
            app_state["toolPermissionContext"],
            command_subcommand_prefix,
            compound_command_has_cd,
            ast_subcommands is not None,
        )
        if result["behavior"] in ("ask", "passthrough"):
            return {**result}
        return result

    # Multiple subcommands: collect per-subcommand results.
    subcommand_results: dict[str, dict[str, Any]] = {}
    for subcommand in subcommands:
        sub_prefix = (
            command_subcommand_prefix["subcommandPrefixes"].get(subcommand)
            if command_subcommand_prefix
            else None
        )
        subcommand_results[subcommand] = await check_command_and_suggest_rules(
            {**_input_dict(input), "command": subcommand},
            app_state["toolPermissionContext"],
            sub_prefix,
            compound_command_has_cd,
            ast_subcommands is not None,
        )

    if all(
        subcommand_results.get(subcommand, {}).get("behavior") == "allow"
        for subcommand in subcommands
    ):
        return {
            "behavior": "allow",
            "updatedInput": input,
            "decisionReason": {
                "type": "subcommandResults",
                "reasons": subcommand_results,
            },
        }

    # Otherwise, ask for permission — collect suggested rules.
    collected_rules: dict[str, Any] = {}
    for subcommand, permission_result in subcommand_results.items():
        if permission_result["behavior"] in ("ask", "passthrough"):
            updates = permission_result.get("suggestions")
            rules = extract_rules(updates)
            for rule in rules:
                rule_key = permission_rule_value_to_string(rule)
                collected_rules[rule_key] = rule

            if (
                permission_result["behavior"] == "ask"
                and len(rules) == 0
                and permission_result.get("decisionReason", {}).get("type") != "rule"
            ):
                for rule in extract_rules(_suggestion_for_exact_command(subcommand)):
                    rule_key = permission_rule_value_to_string(rule)
                    collected_rules[rule_key] = rule

    decision_reason = {"type": "subcommandResults", "reasons": subcommand_results}

    capped_rules = list(collected_rules.values())[:MAX_SUGGESTED_RULES_FOR_COMPOUND]
    suggested_updates = (
        [
            {
                "type": "addRules",
                "rules": capped_rules,
                "behavior": "allow",
                "destination": "localSettings",
            }
        ]
        if len(capped_rules) > 0
        else None
    )
    return {
        "behavior": "ask" if ask_subresult is not None else "passthrough",
        "message": create_permission_request_message(BASH_TOOL_NAME, decision_reason),
        "decisionReason": decision_reason,
        "suggestions": suggested_updates,
    }


# ---------------------------------------------------------------------------
# Normalized command detection
# ---------------------------------------------------------------------------


def is_normalized_git_command(command: str) -> bool:
    """Git after stripping wrappers/quotes."""
    if command.startswith("git ") or command == "git":
        return True
    stripped = strip_safe_wrappers(command)
    parsed = try_parse_shell_command(stripped)
    if parsed["success"] and len(parsed["tokens"]) > 0:
        if parsed["tokens"][0] == "git":
            return True
        if parsed["tokens"][0] == "xargs" and "git" in parsed["tokens"]:
            return True
        return False
    return bool(re.match(r"^git(?:\s|$)", stripped))


def is_normalized_cd_command(command: str) -> bool:
    """Cd/pushd/popd after stripping wrappers."""
    stripped = strip_safe_wrappers(command)
    parsed = try_parse_shell_command(stripped)
    if parsed["success"] and len(parsed["tokens"]) > 0:
        cmd = parsed["tokens"][0]
        return cmd in ("cd", "pushd", "popd")
    return bool(re.match(r"^(?:cd|pushd|popd)(?:\s|$)", stripped))


def command_has_any_cd(command: str) -> bool:
    """Any normalized cd in a compound command."""
    return any(
        is_normalized_cd_command(subcmd.strip())
        for subcmd in split_command_deprecated(command)
    )


# ---------------------------------------------------------------------------
# Input accessors — the TS input is a Zod object; here it may be a pydantic
# model OR a plain dict (subcommand recursion passes {"command": ...} dicts).
# ---------------------------------------------------------------------------


def _input_command(input: Any) -> str:
    if isinstance(input, dict):
        return input.get("command", "")
    return getattr(input, "command", "")


def _input_dict(input: Any) -> dict[str, Any]:
    if isinstance(input, dict):
        return dict(input)
    if hasattr(input, "model_dump"):
        try:
            return input.model_dump()
        except Exception:  # noqa: BLE001 - best-effort spread
            pass
    return {"command": _input_command(input)}
