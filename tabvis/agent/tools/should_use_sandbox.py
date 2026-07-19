"""Sandbox-decision for the Bash tool.

``should_use_sandbox`` decides whether a given bash command should run inside the
sandbox. It honors the global sandbox enablement, the per-call
``dangerouslyDisableSandbox`` override (only when policy allows unsandboxed
commands), and the user-configured ``sandbox.excludedCommands`` convenience list.

NOTE: ``excludedCommands`` is a user-facing convenience feature, **not** a
security boundary — bypassing it is not a security bug; the sandbox permission
system (which prompts the user) is the actual control.

Cycle note: ``tabvis.agent.tools.bash_permissions`` references ``should_use_sandbox`` (via
``bash_tool``), so this module imports ``bash_permissions`` **lazily** (function-
local) — a top-level import would create a cycle and break import-smoke when the
sibling isn't on disk yet.
"""

from __future__ import annotations

import re
from typing import Any, TypedDict
from tabvis.utils.bash.commands import split_command_deprecated
from tabvis.utils.sandbox.sandbox_adapter import SandboxManager
from tabvis.utils.settings.settings import get_initial_settings


class SandboxInput(TypedDict, total=False):
    command: str
    dangerouslyDisableSandbox: bool


def _contains_excluded_command(command: str) -> bool:
    """True if any subcommand matches an excluded pattern (ant dynamic-config list or
    user-configured settings list)."""
    # lazy import to break the bash_permissions <-> should_use_sandbox cycle.
    from tabvis.agent.tools.bash_permissions import (  # noqa: PLC0415
        BINARY_HIJACK_VARS,
        bash_permission_rule,
        match_wildcard_pattern,
        strip_all_leading_env_vars,
        strip_safe_wrappers)

    # Check user-configured excluded commands from settings.
    settings = get_initial_settings()
    sandbox_settings = _get(settings, "sandbox") or {}
    user_excluded_commands = _get(sandbox_settings, "excludedCommands") or []

    if len(user_excluded_commands) == 0:
        return False

    # Split compound commands into individual subcommands and check each against
    # excluded patterns (prevents a compound command from escaping the sandbox
    # because only its first subcommand matched).
    try:
        subcommands = split_command_deprecated(command)
    except Exception:  # noqa: BLE001
        subcommands = [command]

    for subcommand in subcommands:
        trimmed = subcommand.strip()
        # Also try matching with env-var prefixes and wrapper commands stripped,
        # so `FOO=bar bazel ...` and `timeout 30 bazel ...` match `bazel:*`.
        # Iteratively apply both stripping operations to a fixed point.
        candidates: list[str] = [trimmed]
        seen: set[str] = set(candidates)
        start_idx = 0
        while start_idx < len(candidates):
            end_idx = len(candidates)
            for i in range(start_idx, end_idx):
                cmd = candidates[i]
                env_stripped = strip_all_leading_env_vars(cmd, BINARY_HIJACK_VARS)
                if env_stripped not in seen:
                    candidates.append(env_stripped)
                    seen.add(env_stripped)
                wrapper_stripped = strip_safe_wrappers(cmd)
                if wrapper_stripped not in seen:
                    candidates.append(wrapper_stripped)
                    seen.add(wrapper_stripped)
            start_idx = end_idx

        for pattern in user_excluded_commands:
            rule = bash_permission_rule(pattern)
            for cand in candidates:
                rule_type = _get(rule, "type")
                if rule_type == "prefix":
                    prefix = _get(rule, "prefix")
                    if cand == prefix or cand.startswith(str(prefix) + " "):
                        return True
                elif rule_type == "exact":
                    if cand == _get(rule, "command"):
                        return True
                elif rule_type == "wildcard":
                    if match_wildcard_pattern(_get(rule, "pattern"), cand):
                        return True

    return False


def should_use_sandbox(input: SandboxInput | dict[str, Any] | Any) -> bool:
    """Decide whether the given bash command should run inside the sandbox."""
    if not SandboxManager.is_sandboxing_enabled():
        return False

    # Don't sandbox if explicitly overridden AND unsandboxed commands are allowed
    # by policy.
    if _get(input, "dangerouslyDisableSandbox") and (
        SandboxManager.are_unsandboxed_commands_allowed()
    ):
        return False

    command = _get(input, "command")
    if not command:
        return False

    # Don't sandbox if the command contains user-configured excluded commands.
    if _contains_excluded_command(command):
        return False

    return True


def _matches(pattern: Any, value: str) -> bool:
    """Apply a regex/string blocklist pattern — defensive helper (unused by default)."""
    if pattern is None:
        return False
    if isinstance(pattern, re.Pattern):
        return pattern.search(value) is not None
    return bool(re.search(str(pattern), value))


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from a pydantic model or a plain dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)
