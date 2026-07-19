"""Permission setup / initialisation.

Assembles the initial :class:`~tabvis.types.permissions.ToolPermissionContext` from CLI flags +
settings + on-disk rules, plus the dangerous-shell-rule detectors
(``is_dangerous_{bash,powershell}_permission``, ``find_overly_broad_*_permissions``,
``remove_dangerous_permissions``), the CLI tool-list parsers, and the bypassPermissions-mode
gating helpers.

Casing: Python identifiers are snake_case; the dict-shaped permission-rule / context / update
payloads round-trip to settings / transcript / SDK JSON, so wire keys are kept verbatim
(``toolName`` / ``ruleContent`` / ``behavior`` / ``destination`` / ``source`` / ``path`` /
``mode`` / ``type`` …).

Flat-tools architecture: ``BASH_TOOL_NAME`` is imported from the flat ``tabvis.agent.tools.bash_tool``
(it lives at ``bash_tool.py:51``). ``POWERSHELL_TOOL_NAME`` has no flat home
(``tabvis/tools/power_shell_tool.py`` does not exist), so the tiny constant is inlined here as
``"PowerShell"``.

Local implementations (permanent in this build):

* :func:`apply_permission_rules_to_permission_context` is implemented locally in this module rather
  than in :mod:`tabvis.utils.permissions.permissions`. Its body is trivial —
  ``apply_permission_updates(ctx, _convert_rules_to_updates(rules, 'addRules'))``.
* :func:`_parse_tool_preset` is implemented locally here (a lowercase + a one-element
  ``TOOL_PRESETS`` membership check); ``get_tools_for_default_preset`` lives in
  ``tabvis.agent.tools``.

Cycle break: ``tabvis.agent.tools`` (the registry) lazy-imports the query loop, which imports
``tabvis.agent.tools`` back, so the registry helpers used here (``get_tools_for_default_preset``) are
imported function-locally rather than at module scope.
"""

from __future__ import annotations

import os
import os.path
import stat as stat_module
from typing import Any, TypedDict

from tabvis.bootstrap.state import get_original_cwd
from tabvis.agent.tools.bash_tool import BASH_TOOL_NAME
from tabvis.types.permissions import (
    AdditionalWorkingDirectory,
    PermissionMode,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    PermissionUpdateDestination,
    ToolPermissionContext,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.fs_operations import get_fs_implementation, safe_resolve_path
from tabvis.utils.graceful_shutdown import graceful_shutdown
from tabvis.utils.permissions.dangerous_patterns import (
    CROSS_PLATFORM_CODE_EXEC,
    DANGEROUS_BASH_PATTERNS,
)
from tabvis.utils.permissions.permission_mode import permission_mode_from_string
from tabvis.utils.permissions.permission_rule_parser import (
    normalize_legacy_tool_name,
    permission_rule_value_from_string,
    permission_rule_value_to_string,
)
from tabvis.utils.permissions.permission_update import (
    apply_permission_update,
    apply_permission_updates,
)
from tabvis.utils.permissions.permissions_loader import (
    load_all_permission_rules_from_disk,
)
from tabvis.utils.settings.constants import (
    SETTING_SOURCES,
    SettingSource,
    get_settings_file_path_for_source,
)
from tabvis.utils.settings.settings import get_settings_with_errors

# (flat-tools architecture; no PowerShell tool module). Faithful copy of
# ``src/tools/PowerShellTool/toolName.ts`` (``export const POWERSHELL_TOOL_NAME = 'PowerShell'``).
POWERSHELL_TOOL_NAME = "PowerShell"

__all__ = [
    "POWERSHELL_TOOL_NAME",
    "AddDirValidationResult",
    "DangerousPermissionInfo",
    "check_and_disable_bypass_permissions",
    "create_disabled_bypass_permissions_context",
    "find_overly_broad_bash_permissions",
    "find_overly_broad_power_shell_permissions",
    "initial_permission_mode_from_cli",
    "initialize_tool_permission_context",
    "is_bypass_permissions_mode_disabled",
    "is_dangerous_bash_permission",
    "is_dangerous_power_shell_permission",
    "is_overly_broad_bash_allow_rule",
    "is_overly_broad_power_shell_allow_rule",
    "parse_base_tools_from_cli",
    "parse_tool_list_from_cli",
    "prepare_context_for_plan_mode",
    "remove_dangerous_permissions",
    "should_disable_bypass_permissions",
]


def _get_settings_deprecated() -> dict[str, Any]:
    """Return the settings deprecated().

    Returns the merged settings as a plain dict with the camelCase wire keys preserved
    (``permissions.defaultMode`` / ``permissions.disableBypassPermissionsMode`` /
    ``permissions.additionalDirectories``), matching the TS object the original module reads.
    """
    return get_settings_with_errors().get("settings") or {}


# ---------------------------------------------------------------------------
# Additional-working-directory validation
# ---------------------------------------------------------------------------


class AddDirValidationResult(TypedDict, total=False):
    """Discriminated-union result of validating an --add-dir path.

    ``resultType`` ∈ {``success``, ``pathNotFound``, ``notDirectory``,
    ``alreadyInWorkingDirectory``}; ``absolutePath`` set on success, ``path`` otherwise.
    """

    resultType: str  # required
    absolutePath: str
    path: str


async def _validate_directory_for_workspace(
    directory: str,
    _tool_permission_context: ToolPermissionContext,
) -> AddDirValidationResult:
    # NB: faithful to the TS, which calls safeResolvePath(getCwd(), dir) and treats the result as
    # a string. The implemented safe_resolve_path(fs, file_path) returns a dict; here we resolve the
    # path against cwd with os.path. The TS stat() THROWS on a missing path (-> pathNotFound)
    # before the isDirectory() check; os.stat() raises the same way, preserving that control flow
    # (so a missing dir is silently skipped, but an existing non-directory is flagged).
    absolute_path = os.path.abspath(os.path.join(get_cwd(), directory))
    try:
        st = os.stat(absolute_path)
        if not stat_module.S_ISDIR(st.st_mode):
            return {"resultType": "notDirectory", "path": absolute_path}
        return {"resultType": "success", "absolutePath": absolute_path}
    except OSError:
        return {"resultType": "pathNotFound", "path": absolute_path}


def _add_dir_help_message(result: AddDirValidationResult) -> str:
    result_type = result.get("resultType")
    if result_type == "notDirectory":
        return f"Additional workspace path is not a directory: {result.get('path')}"
    if result_type == "pathNotFound":
        return f"Additional workspace path was not found: {result.get('path')}"
    if result_type == "alreadyInWorkingDirectory":
        return f"Additional workspace path is already covered: {result.get('path')}"
    return ""


# ---------------------------------------------------------------------------
# Dangerous shell-rule detection
# ---------------------------------------------------------------------------


def is_dangerous_bash_permission(
    tool_name: str,
    rule_content: str | None,
) -> bool:
    """Whether a Bash permission rule broadly allows arbitrary-code execution."""
    # Only check Bash rules.
    if tool_name != BASH_TOOL_NAME:
        return False

    # Tool-level allow (Bash with no content, or Bash(*)) — allows ALL commands.
    if rule_content is None or rule_content == "":
        return True

    content = rule_content.strip().lower()

    # Standalone wildcard (*) matches everything.
    if content == "*":
        return True

    # Check for dangerous patterns with prefix syntax (e.g. "python:*") or wildcard ("python*").
    for pattern in DANGEROUS_BASH_PATTERNS:
        lower_pattern = pattern.lower()

        # Exact match to the pattern itself (e.g. "python" as a rule).
        if content == lower_pattern:
            return True
        # Prefix syntax: "python:*" allows any python command.
        if content == f"{lower_pattern}:*":
            return True
        # Wildcard at end: "python*" matches python, python3, etc.
        if content == f"{lower_pattern}*":
            return True
        # Wildcard with space: "python *" would match "python script.py".
        if content == f"{lower_pattern} *":
            return True
        # Patterns like "python -*" which would match "python -c 'code'".
        if content.startswith(f"{lower_pattern} -") and content.endswith("*"):
            return True

    return False


def is_dangerous_power_shell_permission(
    tool_name: str,
    rule_content: str | None,
) -> bool:
    """Whether a PowerShell permission rule broadly allows arbitrary-code execution.

    PowerShell is case-insensitive, so rule content is lowercased before matching.
    """
    if tool_name != POWERSHELL_TOOL_NAME:
        return False

    # Tool-level allow (PowerShell with no content, or PowerShell(*)) — allows ALL commands.
    if rule_content is None or rule_content == "":
        return True

    content = rule_content.strip().lower()

    # Standalone wildcard (*) matches everything.
    if content == "*":
        return True

    # PS-specific cmdlet names. CROSS_PLATFORM_CODE_EXEC is shared with bash.
    patterns: tuple[str, ...] = (
        *CROSS_PLATFORM_CODE_EXEC,
        # Nested PS + shells launchable from PS
        "pwsh",
        "powershell",
        "cmd",
        "wsl",
        # String/scriptblock evaluators
        "iex",
        "invoke-expression",
        "icm",
        "invoke-command",
        # Process spawners
        "start-process",
        "saps",
        "start",
        "start-job",
        "sajb",
        "start-threadjob",  # bundled PS 6.1+; takes -ScriptBlock like Start-Job
        # Event/session code exec
        "register-objectevent",
        "register-engineevent",
        "register-wmievent",
        "register-scheduledjob",
        "new-pssession",
        "nsn",  # alias
        "enter-pssession",
        "etsn",  # alias
        # .NET escape hatches
        "add-type",  # Add-Type -TypeDefinition '<C#>' -> P/Invoke
        "new-object",  # New-Object -ComObject WScript.Shell -> .Run()
    )

    for pattern in patterns:
        # patterns stored lowercase; content lowercased above.
        if content == pattern:
            return True
        if content == f"{pattern}:*":
            return True
        if content == f"{pattern}*":
            return True
        if content == f"{pattern} *":
            return True
        if content.startswith(f"{pattern} -") and content.endswith("*"):
            return True
        # .exe — goes on the FIRST word. `python` -> `python.exe`.
        # `npm run` -> `npm.exe run` (npm.exe is the real Windows binary name).
        # A rule like `PowerShell(npm.exe run:*)` needs to match `npm run`.
        sp = pattern.find(" ")
        exe = (
            f"{pattern}.exe"
            if sp == -1
            else f"{pattern[:sp]}.exe{pattern[sp:]}"
        )
        if content == exe:
            return True
        if content == f"{exe}:*":
            return True
        if content == f"{exe}*":
            return True
        if content == f"{exe} *":
            return True
        if content.startswith(f"{exe} -") and content.endswith("*"):
            return True
    return False


def _format_permission_source(source: PermissionRuleSource) -> str:
    if source in SETTING_SOURCES:
        file_path = get_settings_file_path_for_source(source)  # type: ignore[arg-type]
        if file_path:
            relative_path = os.path.relpath(file_path, get_cwd())
            return relative_path if len(relative_path) < len(file_path) else file_path
    return source


class DangerousPermissionInfo(TypedDict):
    ruleValue: PermissionRuleValue
    source: PermissionRuleSource
    # The permission rule formatted for display, e.g. "Bash(*)" or "Bash(python:*)".
    ruleDisplay: str
    # The source formatted for display, e.g. a file path or "--allowed-tools".
    sourceDisplay: str


def is_overly_broad_bash_allow_rule(rule_value: PermissionRuleValue) -> bool:
    """Whether a Bash allow rule is overly broad (equivalent to YOLO mode).

    Matches Bash / Bash(*) / Bash() — all parse to ``{toolName: 'Bash'}`` with no ruleContent.
    """
    return (
        rule_value.get("toolName") == BASH_TOOL_NAME
        and rule_value.get("ruleContent") is None
    )


def is_overly_broad_power_shell_allow_rule(rule_value: PermissionRuleValue) -> bool:
    """PowerShell equivalent of :func:`is_overly_broad_bash_allow_rule`."""
    return (
        rule_value.get("toolName") == POWERSHELL_TOOL_NAME
        and rule_value.get("ruleContent") is None
    )


def find_overly_broad_bash_permissions(
    rules: list[PermissionRule],
    cli_allowed_tools: list[str],
) -> list[DangerousPermissionInfo]:
    """Find every overly broad Bash allow rule (Bash / Bash(*)) from settings + CLI args."""
    overly_broad: list[DangerousPermissionInfo] = []

    for rule in rules:
        if rule.get("ruleBehavior") == "allow" and is_overly_broad_bash_allow_rule(
            rule["ruleValue"]
        ):
            overly_broad.append(
                {
                    "ruleValue": rule["ruleValue"],
                    "source": rule["source"],
                    "ruleDisplay": f"{BASH_TOOL_NAME}(*)",
                    "sourceDisplay": _format_permission_source(rule["source"]),
                }
            )

    for tool_spec in cli_allowed_tools:
        parsed = permission_rule_value_from_string(tool_spec)
        if is_overly_broad_bash_allow_rule(parsed):
            overly_broad.append(
                {
                    "ruleValue": parsed,
                    "source": "cliArg",
                    "ruleDisplay": f"{BASH_TOOL_NAME}(*)",
                    "sourceDisplay": "--allowed-tools",
                }
            )

    return overly_broad


def find_overly_broad_power_shell_permissions(
    rules: list[PermissionRule],
    cli_allowed_tools: list[str],
) -> list[DangerousPermissionInfo]:
    """PowerShell equivalent of :func:`find_overly_broad_bash_permissions`."""
    overly_broad: list[DangerousPermissionInfo] = []

    for rule in rules:
        if rule.get("ruleBehavior") == "allow" and is_overly_broad_power_shell_allow_rule(
            rule["ruleValue"]
        ):
            overly_broad.append(
                {
                    "ruleValue": rule["ruleValue"],
                    "source": rule["source"],
                    "ruleDisplay": f"{POWERSHELL_TOOL_NAME}(*)",
                    "sourceDisplay": _format_permission_source(rule["source"]),
                }
            )

    for tool_spec in cli_allowed_tools:
        parsed = permission_rule_value_from_string(tool_spec)
        if is_overly_broad_power_shell_allow_rule(parsed):
            overly_broad.append(
                {
                    "ruleValue": parsed,
                    "source": "cliArg",
                    "ruleDisplay": f"{POWERSHELL_TOOL_NAME}(*)",
                    "sourceDisplay": "--allowed-tools",
                }
            )

    return overly_broad


def _is_permission_update_destination(source: PermissionRuleSource) -> bool:
    """Whether ``source`` is a valid :data:`PermissionUpdateDestination`.

    Sources like ``flagSettings`` / ``policySettings`` / ``command`` are not valid destinations.
    """
    return source in (
        "userSettings",
        "projectSettings",
        "localSettings",
        "session",
        "cliArg",
    )


def remove_dangerous_permissions(
    context: ToolPermissionContext,
    dangerous_permissions: list[DangerousPermissionInfo],
) -> ToolPermissionContext:
    """Remove dangerous permissions from the in-memory context (grouped by source)."""
    # Group dangerous rules by their source (destination for updates).
    rules_by_source: dict[PermissionUpdateDestination, list[PermissionRuleValue]] = {}
    for perm in dangerous_permissions:
        # Skip sources that can't be persisted (flagSettings, policySettings, command).
        if not _is_permission_update_destination(perm["source"]):
            continue
        destination: PermissionUpdateDestination = perm["source"]  # type: ignore[assignment]
        existing = rules_by_source.get(destination, [])
        existing.append(perm["ruleValue"])
        rules_by_source[destination] = existing

    updated_context = context
    for destination, rules in rules_by_source.items():
        updated_context = apply_permission_update(
            updated_context,
            {
                "type": "removeRules",
                "rules": rules,
                "behavior": "allow",
                "destination": destination,
            },
        )

    return updated_context


# ---------------------------------------------------------------------------
# CLI tool-list parsing
# ---------------------------------------------------------------------------


def _parse_tool_preset(preset: str) -> str | None:
    """Parse a tool-preset name (``TOOL_PRESETS`` contains only ``'default'``)."""
    # TOOL_PRESETS contains only 'default'.
    preset_string = preset.lower()
    if preset_string not in ("default",):
        return None
    return preset_string


def parse_base_tools_from_cli(base_tools: list[str]) -> list[str]:
    """Parse the base-tools spec from the CLI (preset name or custom tool list)."""
    # Cycle-safe lazy import of the tools registry.
    from tabvis.agent.tools import get_tools_for_default_preset

    # Join all array elements and check if it's a single preset name.
    joined_input = " ".join(base_tools).strip()
    preset = _parse_tool_preset(joined_input)

    if preset:
        return get_tools_for_default_preset()

    # Parse as a custom tool list using the same logic as allowedTools/disallowedTools.
    return parse_tool_list_from_cli(base_tools)


def _is_symlink_to(process_pwd: str, original_cwd: str) -> bool:
    """Whether ``process_pwd`` is a symlink that resolves to ``original_cwd``."""
    resolved = safe_resolve_path(get_fs_implementation(), process_pwd)
    resolved_process_pwd = resolved["resolved_path"]
    is_process_pwd_symlink = resolved["is_symlink"]
    return (
        resolved_process_pwd == os.path.abspath(original_cwd)
        if is_process_pwd_symlink
        else False
    )


class _InitialPermissionModeResult(TypedDict, total=False):
    mode: PermissionMode  # required
    notification: str


def initial_permission_mode_from_cli(
    permission_mode_cli: str | None,
    dangerously_skip_permissions: bool | None,
) -> _InitialPermissionModeResult:
    """Safely convert CLI flags to a :data:`PermissionMode` (+ an optional notification)."""
    settings = _get_settings_deprecated()

    # Check GrowthBook gate first — highest precedence.
    growth_book_disable_bypass_permissions_mode = False

    # Then check settings — lower precedence.
    settings_disable_bypass_permissions_mode = (
        (settings.get("permissions") or {}).get("disableBypassPermissionsMode") == "disable"
    )

    # Statsig gate takes precedence over settings.
    disable_bypass_permissions_mode = (
        growth_book_disable_bypass_permissions_mode
        or settings_disable_bypass_permissions_mode
    )

    # Modes in order of priority.
    ordered_modes: list[PermissionMode] = []
    notification: str | None = None

    if dangerously_skip_permissions:
        ordered_modes.append("bypassPermissions")
    if permission_mode_cli:
        parsed_mode = permission_mode_from_string(permission_mode_cli)
        ordered_modes.append(parsed_mode)
    settings_default_mode = (settings.get("permissions") or {}).get("defaultMode")
    if settings_default_mode:
        settings_mode: PermissionMode = settings_default_mode
        # CCR only supports acceptEdits and plan — ignore other defaultModes from settings
        # (e.g. bypassPermissions would otherwise silently grant full access remotely).
        if is_env_truthy(os.environ.get("TABVIS_REMOTE")) and settings_mode not in (
            "acceptEdits",
            "plan",
            "default",
        ):
            log_for_debugging(
                f'settings defaultMode "{settings_mode}" is not supported in TABVIS_REMOTE — '
                "only acceptEdits and plan are allowed",
                {"level": "warn"},
            )
        else:
            ordered_modes.append(settings_mode)

    result: _InitialPermissionModeResult | None = None

    for mode in ordered_modes:
        if mode == "bypassPermissions" and disable_bypass_permissions_mode:
            if growth_book_disable_bypass_permissions_mode:
                log_for_debugging(
                    "bypassPermissions mode is disabled by Statsig gate",
                    {"level": "warn"},
                )
                notification = (
                    "Bypass permissions mode was disabled by your organization policy"
                )
            else:
                log_for_debugging(
                    "bypassPermissions mode is disabled by settings",
                    {"level": "warn"},
                )
                notification = "Bypass permissions mode was disabled by settings"
            continue  # Skip this mode if it's disabled.

        result = {"mode": mode}  # Use the first valid mode.
        if notification is not None:
            result["notification"] = notification
        break

    if result is None:
        result = {"mode": "default"}
        if notification is not None:
            result["notification"] = notification

    return result


def parse_tool_list_from_cli(tools: list[str]) -> list[str]:
    """Split a comma/space-separated tool list, respecting parenthesised content."""
    if len(tools) == 0:
        return []

    result: list[str] = []

    # Process each string in the array.
    for tool_string in tools:
        if not tool_string:
            continue

        current = ""
        is_in_parens = False

        # Parse each character in the string.
        for char in tool_string:
            if char == "(":
                is_in_parens = True
                current += char
            elif char == ")":
                is_in_parens = False
                current += char
            elif char == ",":
                if is_in_parens:
                    current += char
                else:
                    # Comma separator — push current tool and start new one.
                    if current.strip():
                        result.append(current.strip())
                    current = ""
            elif char == " ":
                if is_in_parens:
                    current += char
                elif current.strip():
                    # Space separator — push current tool and start new one.
                    result.append(current.strip())
                    current = ""
            else:
                current += char

        # Push any remaining tool.
        if current.strip():
            result.append(current.strip())

    return result


# ---------------------------------------------------------------------------
# applyPermissionRulesToPermissionContext behavior
# ---------------------------------------------------------------------------


def _convert_rules_to_updates(
    rules: list[PermissionRule],
    update_type: str,
) -> list[dict[str, Any]]:
    """Group rules by ``(source, behavior)`` into ``addRules`` / ``replaceRules`` updates.

        ``addRules`` path is used by :func:`apply_permission_rules_to_permission_context`).
    """
    grouped: dict[tuple[str, str], list[PermissionRuleValue]] = {}
    order: list[tuple[str, str]] = []
    for rule in rules:
        key = (rule["source"], rule["ruleBehavior"])
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(rule["ruleValue"])

    updates: list[dict[str, Any]] = []
    for source, behavior in order:
        updates.append(
            {
                "type": update_type,
                "rules": grouped[(source, behavior)],
                "behavior": behavior,
                "destination": source,
            }
        )
    return updates


def apply_permission_rules_to_permission_context(
    tool_permission_context: ToolPermissionContext,
    rules: list[PermissionRule],
) -> ToolPermissionContext:
    """Apply permission rules to context (additive — for initial setup).

    Implemented locally in this module.
    """
    updates = _convert_rules_to_updates(rules, "addRules")
    return apply_permission_updates(tool_permission_context, updates)


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class InitializeToolPermissionContextResult(TypedDict):
    toolPermissionContext: ToolPermissionContext
    warnings: list[str]
    overlyBroadBashPermissions: list[DangerousPermissionInfo]


async def initialize_tool_permission_context(
    allowed_tools_cli: list[str],
    disallowed_tools_cli: list[str],
    permission_mode: PermissionMode,
    allow_dangerously_skip_permissions: bool,
    add_dirs: list[str],
    base_tools_cli: list[str] | None = None,
) -> InitializeToolPermissionContextResult:
    """Assemble the initial :class:`ToolPermissionContext` from CLI flags + settings + disk rules."""
    # Cycle-safe lazy import of the tools registry.
    from tabvis.agent.tools import get_tools_for_default_preset

    # Parse comma-separated allowed/disallowed tools if provided.
    parsed_allowed_tools_cli = [
        permission_rule_value_to_string(permission_rule_value_from_string(rule))
        for rule in parse_tool_list_from_cli(allowed_tools_cli)
    ]
    parsed_disallowed_tools_cli = parse_tool_list_from_cli(disallowed_tools_cli)

    # If base tools are specified, automatically deny all tools NOT in the base set. We need to
    # check if base tools were explicitly provided (not just empty default).
    if base_tools_cli and len(base_tools_cli) > 0:
        base_tools_result = parse_base_tools_from_cli(base_tools_cli)
        # Normalize legacy tool names (e.g. 'Task' -> 'Agent') so user-provided base tool lists
        # using old names still match canonical names.
        base_tools_set = {normalize_legacy_tool_name(t) for t in base_tools_result}
        all_tool_names = get_tools_for_default_preset()
        tools_to_disallow = [t for t in all_tool_names if t not in base_tools_set]
        parsed_disallowed_tools_cli = [*parsed_disallowed_tools_cli, *tools_to_disallow]

    warnings: list[str] = []
    additional_working_directories: dict[str, AdditionalWorkingDirectory] = {}
    # process.env.PWD may be a symlink, while get_original_cwd() uses the real path.
    process_pwd = os.environ.get("PWD")
    if (
        process_pwd
        and process_pwd != get_original_cwd()
        and _is_symlink_to(process_pwd, get_original_cwd())
    ):
        additional_working_directories[process_pwd] = {
            "path": process_pwd,
            "source": "session",
        }

    # Check if bypassPermissions mode is available (not disabled by Statsig gate or settings).
    # Use cached values to avoid blocking on startup.
    growth_book_disable_bypass_permissions_mode = False
    settings = _get_settings_deprecated()
    settings_disable_bypass_permissions_mode = (
        (settings.get("permissions") or {}).get("disableBypassPermissionsMode") == "disable"
    )
    is_bypass_permissions_mode_available = (
        (permission_mode == "bypassPermissions" or allow_dangerously_skip_permissions)
        and not growth_book_disable_bypass_permissions_mode
        and not settings_disable_bypass_permissions_mode
    )

    # Load all permission rules from disk.
    rules_from_disk = load_all_permission_rules_from_disk()

    # Ant-only: detect overly broad shell allow rules for all modes. Bash(*) or PowerShell(*) are
    # equivalent to YOLO mode for that shell. Skip in CCR/BYOC where --allowed-tools is the
    # intended pre-approval mechanism. Variable name kept for return-field compat; contains both.
    overly_broad_bash_permissions: list[DangerousPermissionInfo] = []

    tool_permission_context: ToolPermissionContext = apply_permission_rules_to_permission_context(
        {
            "mode": permission_mode,
            "additionalWorkingDirectories": additional_working_directories,
            "alwaysAllowRules": {"cliArg": parsed_allowed_tools_cli},
            "alwaysDenyRules": {"cliArg": parsed_disallowed_tools_cli},
            "alwaysAskRules": {},
            "isBypassPermissionsModeAvailable": is_bypass_permissions_mode_available,
        },
        rules_from_disk,
    )

    # Add directories from settings and --add-dir.
    all_additional_directories = [
        *((settings.get("permissions") or {}).get("additionalDirectories") or []),
        *add_dirs,
    ]
    # Validate fs then apply updates serially (cumulative context). validate only reads the
    # context to check coverage — the behavioural difference from parallelising is benign.
    validation_results = [
        await _validate_directory_for_workspace(directory, tool_permission_context)
        for directory in all_additional_directories
    ]
    for result in validation_results:
        if result.get("resultType") == "success":
            tool_permission_context = apply_permission_update(
                tool_permission_context,
                {
                    "type": "addDirectories",
                    "directories": [result["absolutePath"]],
                    "destination": "cliArg",
                },
            )
        elif result.get("resultType") not in ("alreadyInWorkingDirectory", "pathNotFound"):
            # Warn for actual config mistakes (e.g. specifying a file instead of a directory).
            # If the directory doesn't exist anymore, silently skip.
            warnings.append(_add_dir_help_message(result))

    return {
        "toolPermissionContext": tool_permission_context,
        "warnings": warnings,
        "overlyBroadBashPermissions": overly_broad_bash_permissions,
    }


# ---------------------------------------------------------------------------
# bypassPermissions gating
# ---------------------------------------------------------------------------


async def should_disable_bypass_permissions() -> bool:
    """Core logic: whether bypassPermissions should be disabled based on the Statsig gate."""
    return False


def is_bypass_permissions_mode_disabled() -> bool:
    """Whether bypassPermissions mode is currently disabled by Statsig gate or settings.

    Synchronous version that uses cached Statsig values.
    """
    growth_book_disable_bypass_permissions_mode = False
    settings = _get_settings_deprecated()
    settings_disable_bypass_permissions_mode = (
        (settings.get("permissions") or {}).get("disableBypassPermissionsMode") == "disable"
    )

    return (
        growth_book_disable_bypass_permissions_mode
        or settings_disable_bypass_permissions_mode
    )


def create_disabled_bypass_permissions_context(
    current_context: ToolPermissionContext,
) -> ToolPermissionContext:
    """Create an updated context with bypassPermissions disabled."""
    updated_context = current_context
    if current_context.get("mode") == "bypassPermissions":
        updated_context = apply_permission_update(
            current_context,
            {
                "type": "setMode",
                "mode": "default",
                "destination": "session",
            },
        )

    return {**updated_context, "isBypassPermissionsModeAvailable": False}


async def check_and_disable_bypass_permissions(
    current_context: ToolPermissionContext,
) -> None:
    """Async check: disable bypassPermissions mode (graceful shutdown) if the gate is enabled."""
    # Only proceed if bypassPermissions mode is available.
    if not current_context.get("isBypassPermissionsModeAvailable"):
        return

    should_disable = await should_disable_bypass_permissions()
    if not should_disable:
        return

    # Gate is enabled, need to disable bypassPermissions mode.
    log_for_debugging(
        "bypassPermissions mode is being disabled by Statsig gate (async check)",
        {"level": "warn"},
    )

    await graceful_shutdown(1, "bypass_permissions_disabled")


def prepare_context_for_plan_mode(
    context: ToolPermissionContext,
) -> ToolPermissionContext:
    """Centralised plan-mode entry. Stashes the current mode as ``prePlanMode``."""
    current_mode = context.get("mode")
    if current_mode == "plan":
        return context
    log_for_debugging(
        f"[prepare_context_for_plan_mode] plain plan entry, prePlanMode={current_mode}",
        {"level": "info"},
    )
    return {**context, "prePlanMode": current_mode}


# Silence unused-import linters.
_ = SettingSource
