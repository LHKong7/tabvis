"""Sandbox adapter for Tabvis settings and tool integration.

Builds the
:class:`SandboxRuntimeConfig` (allow/deny network + filesystem rule set) from the merged
settings, plus the Tabvis-CLI sandbox manager surface (``SandboxManager``) consumed by the
BashTool / ``shouldUseSandbox`` path.

The actual macOS ``sandbox-exec`` / bubblewrap runtime is **not bundled** in this implementation (mirrors
the TS ``BaseSandboxManager`` stub: ``checkDependencies`` reports the runtime is unavailable,
``isSupportedPlatform`` is ``False``, and ``wrapWithSandbox`` is a pass-through). The faithfully
implemented logic here is the **rule construction** (:func:`convert_to_sandbox_runtime_config`,
:func:`resolve_path_pattern_for_sandbox`, :func:`resolve_sandbox_filesystem_path`) and the
settings-driven gating (enabled / platform / dependency / policy-lock checks).

Implementation notes
-------------
* ``getSettings_DEPRECATED`` / ``getInitialSettings`` return a typed ``SettingsJson`` in TS, with
  ``settings?.sandbox?.network?...`` optional-chaining. The implemented ``settings.py`` returns a plain
  ``dict`` from :func:`get_settings_for_source` and a (loose) :class:`SettingsJson` model from
  :func:`get_initial_settings`. We normalise everything to plain ``dict`` and walk it with
  :func:`_dig` to reproduce the ``?.`` semantics 1:1 (missing -> ``None``).
* ``getSettingsRootPathForSource`` / ``updateSettingsForSource`` are not on the existing
  ``settings.py`` surface, so they are reproduced here as faithful local helpers
  (:func:`_get_settings_root_path_for_source`, :func:`_update_settings_for_source`) — the TS file
  itself keeps "local copies to avoid circular dependency".
* ``ripgrepCommand()`` returns ``{rgPath, rgArgs, argv0}`` in TS; the existing
  :func:`ripgrep_command` returns ``(rg_path, rg_args)`` (no ``argv0`` — no embedded binary), so
  ``argv0`` is always ``None`` here.
* ``lodash.memoize`` (zero-arg) -> module-level cached values cleared by :func:`reset` /
  :func:`_clear_memo_caches`.
* ``settingsChangeDetector.subscribe`` -> :data:`settings_change_detector` ``["subscribe"]``.

Casing: Python identifiers snake_case; tool-name constants and the on-disk / runtime-config dict
keys are kept verbatim (they round-trip to the sandbox runtime + settings JSON).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

# Tool-name constants — sourced from the FLAT tool modules (per tabvis-flat-tool-modules), not from
# colliding tool-dir packages.
from tabvis.agent.tools.bash_tool import BASH_TOOL_NAME
from tabvis.agent.tools.file_edit_tool import FILE_EDIT_TOOL_NAME
from tabvis.agent.tools.file_read_tool import FILE_READ_TOOL_NAME
from tabvis.constants.tools import WEB_FETCH_TOOL_NAME

from ...bootstrap.state import (
    get_additional_directories_for_tabvis_md,
    get_cwd_state,
    get_original_cwd,
)
from ..debug import log_for_debugging
from ..errors import get_error_message
from ..path import expand_path
from ..platform import Platform, get_platform
from ..ripgrep import ripgrep_command
from ..settings.change_detector import settings_change_detector
from ..settings.constants import (
    SETTING_SOURCES,
    EditableSettingSource,
    SettingSource,
    get_tabvis_config_home_dir,
    get_settings_file_path_for_source,
)
from ..settings.managed_path import get_managed_settings_drop_in_dir
from ..settings.settings import (
    get_initial_settings,
    get_settings_for_source,
    reset_settings_cache,
)

# ============================================================================
# Public type aliases (Record<string, unknown> -> dict[str, Any]) + small structs
# ============================================================================

FsReadRestrictionConfig = dict[str, Any]
FsWriteRestrictionConfig = dict[str, Any]
IgnoreViolationsConfig = dict[str, Any]
NetworkHostPattern = dict[str, Any]  # {host: str, port?: int, protocol?: str}
NetworkRestrictionConfig = dict[str, Any]
SandboxAskCallback = Callable[[NetworkHostPattern], Awaitable[bool]]
SandboxRuntimeConfig = dict[str, Any]
SandboxViolationEvent = dict[str, Any]  # {operation?, path?, host?, timestamp?}


class SandboxDependencyCheck(dict):
    """``{errors: list[str], warnings: list[str]}`` (dict-shaped to round-trip)."""


class SandboxViolationStore:
    """In-memory store of sandbox violation events."""

    def __init__(self) -> None:
        self._violations: list[SandboxViolationEvent] = []
        self._listeners: set[Callable[[list[SandboxViolationEvent]], None]] = set()

    def subscribe(
        self, listener: Callable[[list[SandboxViolationEvent]], None]
    ) -> Callable[[], None]:
        """Register ``listener`` (called immediately with the current list); returns an unsubscribe."""
        self._listeners.add(listener)
        listener(self._violations)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    def get_total_count(self) -> int:
        """Total number of recorded violations."""
        return len(self._violations)


class _SandboxRuntimeConfigSchema:
    """``SandboxRuntimeConfigSchema`` — opaque pass-through validator (``parse(value) -> value``)."""

    @staticmethod
    def parse(value: Any) -> SandboxRuntimeConfig:
        return value


SandboxRuntimeConfigSchema = _SandboxRuntimeConfigSchema


# ============================================================================
# Base sandbox manager — the not-bundled runtime stub.
# ============================================================================

_empty_violation_store = SandboxViolationStore()


class _BaseSandboxManager:
    """The unbundled sandbox-runtime stub.

    Mirrors the TS ``BaseSandboxManager`` object literal exactly: dependency check reports the
    runtime is not bundled, the platform is unsupported, and ``wrapWithSandbox`` is a pass-through.
    """

    @staticmethod
    def check_dependencies(_options: dict[str, Any] | None = None) -> SandboxDependencyCheck:
        return SandboxDependencyCheck(
            errors=["sandbox runtime is not bundled"], warnings=[]
        )

    @staticmethod
    def is_supported_platform() -> bool:
        return False

    @staticmethod
    async def wrap_with_sandbox(
        command: str,
        _bin_shell: str | None = None,
        _custom_config: dict[str, Any] | None = None,
        _abort_signal: Any | None = None,
    ) -> str:
        return command

    @staticmethod
    async def initialize(
        _runtime_config: SandboxRuntimeConfig | None = None,
        _callback: SandboxAskCallback | None = None,
    ) -> None:
        return None

    @staticmethod
    def update_config(_config: SandboxRuntimeConfig | None = None) -> None:
        return None

    @staticmethod
    async def reset() -> None:
        return None

    @staticmethod
    def get_fs_read_config() -> FsReadRestrictionConfig | None:
        return None

    @staticmethod
    def get_fs_write_config() -> FsWriteRestrictionConfig | None:
        return None

    @staticmethod
    def get_network_restriction_config() -> NetworkRestrictionConfig | None:
        return None

    @staticmethod
    def get_ignore_violations() -> IgnoreViolationsConfig | None:
        return None

    @staticmethod
    def get_allow_unix_sockets() -> list[str] | None:
        return None

    @staticmethod
    def get_allow_local_binding() -> bool | None:
        return None

    @staticmethod
    def get_enable_weaker_nested_sandbox() -> bool | None:
        return None

    @staticmethod
    def get_proxy_port() -> int | None:
        return None

    @staticmethod
    def get_socks_proxy_port() -> int | None:
        return None

    @staticmethod
    def get_linux_http_socket_path() -> str | None:
        return None

    @staticmethod
    def get_linux_socks_socket_path() -> str | None:
        return None

    @staticmethod
    async def wait_for_network_initialization() -> bool:
        return False

    @staticmethod
    def get_sandbox_violation_store() -> SandboxViolationStore:
        return _empty_violation_store

    @staticmethod
    def annotate_stderr_with_sandbox_failures(_command: str, stderr: str) -> str:
        return stderr

    @staticmethod
    def cleanup_after_command() -> None:
        return None


BaseSandboxManager = _BaseSandboxManager


# ============================================================================
# Settings access helpers (mirror the TS optional-chaining over SettingsJson)
# ============================================================================


def _dig(obj: Any, *keys: str) -> Any:
    """Walk a nested ``dict`` by ``keys`` (the TS ``a?.b?.c`` chain). Missing/non-dict -> ``None``."""
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _get_settings_deprecated() -> dict[str, Any]:
    """Return initial settings as a plain dictionary.

    The implemented :func:`get_initial_settings` returns a (loose) :class:`SettingsJson` model; we dump
    it by alias to a plain dict so the ``?.`` chains in this module can walk it uniformly.
    """
    settings = get_initial_settings()
    return settings.model_dump(by_alias=True, exclude_none=True)


def _get_settings_root_path_for_source(source: SettingSource) -> str:
    """Absolute root directory for ``source``'s settings file.

    e.g. for ``$PROJ_DIR/.tabvis/settings.json`` returns ``$PROJ_DIR``. ``flagSettings`` has no
    on-disk path in the skeleton, so it falls back to the original cwd (mirrors the TS fallback).
    """
    if source == "userSettings":
        return os.path.abspath(get_tabvis_config_home_dir())
    # policySettings / projectSettings / localSettings / flagSettings -> original cwd.
    return os.path.abspath(get_original_cwd())


# ============================================================================
# Settings Converter
# ============================================================================

_RULE_RE = re.compile(r"^([^(]+)\(([^)]+)\)$")
_PREFIX_RE = re.compile(r"^(.+):\*$")


def _permission_rule_value_from_string(rule_string: str) -> dict[str, Any]:
    """Local copy of ``permissionRuleValueFromString`` (avoids the permissions import cycle).

    Returns ``{"toolName": str}`` or ``{"toolName": str, "ruleContent": str}`` (wire keys verbatim).
    """
    matches = _RULE_RE.match(rule_string)
    if not matches:
        return {"toolName": rule_string}
    tool_name = matches.group(1)
    rule_content = matches.group(2)
    if not tool_name or not rule_content:
        return {"toolName": rule_string}
    return {"toolName": tool_name, "ruleContent": rule_content}


def _permission_rule_extract_prefix(permission_rule: str) -> str | None:
    """Local copy of ``permissionRuleExtractPrefix`` — ``"foo:*"`` -> ``"foo"`` (else ``None``)."""
    match = _PREFIX_RE.match(permission_rule)
    return match.group(1) if match else None


def resolve_path_pattern_for_sandbox(pattern: str, source: SettingSource) -> str:
    """Resolve a Tabvis permission-rule path pattern for the sandbox runtime.

    Tabvis-specific conventions:

    - ``//path`` -> absolute from filesystem root (becomes ``/path``).
    - ``/path``  -> relative to the settings-file directory (becomes ``$SETTINGS_DIR/path``).
    - ``~/path`` / ``./path`` / ``path`` -> passed through (sandbox runtime normalises them).
    """
    # ``//`` prefix — absolute from root (CC-specific convention).
    if pattern.startswith("//"):
        return pattern[1:]

    # ``/`` prefix — relative to the settings-file directory (CC-specific convention).
    if pattern.startswith("/") and not pattern.startswith("//"):
        root = _get_settings_root_path_for_source(source)
        # ``/foo/**`` becomes ``${root}/foo/**``.
        return os.path.normpath(os.path.join(root, pattern[1:]))

    # ~/path, ./path, path -> pass through unchanged.
    return pattern


def resolve_sandbox_filesystem_path(pattern: str, source: SettingSource) -> str:
    """Resolve a ``sandbox.filesystem.*`` path (allowWrite / denyWrite / ...).

    Unlike permission rules, these use **standard** path
    semantics (``/path`` = absolute as-written, ``~`` expanded here, relative = settings-relative).
    The legacy ``//path`` -> ``/path`` escape is kept for compat (#30067 workaround).
    """
    if pattern.startswith("//"):
        return pattern[1:]
    return expand_path(pattern, _get_settings_root_path_for_source(source))


def should_allow_managed_sandbox_domains_only() -> bool:
    """Return whether policy restricts sandbox domains to the managed allowlist."""
    return (
        _dig(
            get_settings_for_source("policySettings"),
            "sandbox",
            "network",
            "allowManagedDomainsOnly",
        )
        is True
    )


def _should_allow_managed_read_paths_only() -> bool:
    """Return whether policy restricts sandbox reads to managed paths."""
    return (
        _dig(
            get_settings_for_source("policySettings"),
            "sandbox",
            "filesystem",
            "allowManagedReadPathsOnly",
        )
        is True
    )


def convert_to_sandbox_runtime_config(settings: dict[str, Any]) -> SandboxRuntimeConfig:
    """Convert Tabvis settings into a :class:`SandboxRuntimeConfig`.

    ``settings`` is the merged effective settings (plain dict). Builds the network allow/deny
    domain lists (from ``sandbox.network`` + WebFetch ``domain:`` rules) and the filesystem
    allow/deny read/write path sets (from Edit/Read rules + ``sandbox.filesystem.*``), plus the
    ripgrep config. Exported for testing.
    """
    permissions: dict[str, Any] = settings.get("permissions") or {}

    # --- network: extract domains from WebFetch rules -----------------------------------------
    allowed_domains: list[str] = []
    denied_domains: list[str] = []

    if should_allow_managed_sandbox_domains_only():
        # When allowManagedSandboxDomainsOnly is enabled, only use policy-settings domains.
        policy_settings = get_settings_for_source("policySettings")
        for domain in _dig(policy_settings, "sandbox", "network", "allowedDomains") or []:
            allowed_domains.append(domain)
        for rule_string in _dig(policy_settings, "permissions", "allow") or []:
            rule = _permission_rule_value_from_string(rule_string)
            rule_content = rule.get("ruleContent")
            if rule.get("toolName") == WEB_FETCH_TOOL_NAME and (
                rule_content is not None and rule_content.startswith("domain:")
            ):
                allowed_domains.append(rule_content[len("domain:") :])
    else:
        for domain in _dig(settings, "sandbox", "network", "allowedDomains") or []:
            allowed_domains.append(domain)
        for rule_string in permissions.get("allow") or []:
            rule = _permission_rule_value_from_string(rule_string)
            rule_content = rule.get("ruleContent")
            if rule.get("toolName") == WEB_FETCH_TOOL_NAME and (
                rule_content is not None and rule_content.startswith("domain:")
            ):
                allowed_domains.append(rule_content[len("domain:") :])

    for rule_string in permissions.get("deny") or []:
        rule = _permission_rule_value_from_string(rule_string)
        rule_content = rule.get("ruleContent")
        if rule.get("toolName") == WEB_FETCH_TOOL_NAME and (
            rule_content is not None and rule_content.startswith("domain:")
        ):
            denied_domains.append(rule_content[len("domain:") :])

    # --- filesystem: cwd + temp dir always writable -------------------------------------------
    # The temp directory is needed for Shell.ts cwd tracking files.
    from ..permissions.filesystem import get_tabvis_temp_dir

    allow_write: list[str] = [".", get_tabvis_temp_dir()]
    deny_write: list[str] = []
    deny_read: list[str] = []
    allow_read: list[str] = []

    # Always deny writes to settings.json files to prevent sandbox escape.
    settings_paths = [
        p
        for p in (get_settings_file_path_for_source(s) for s in SETTING_SOURCES)
        if p is not None
    ]
    deny_write.extend(settings_paths)
    deny_write.append(get_managed_settings_drop_in_dir())

    # Block settings files in the current working directory if it differs from original.
    cwd = get_cwd_state()
    original_cwd = get_original_cwd()
    if cwd != original_cwd:
        deny_write.append(os.path.normpath(os.path.join(cwd, ".tabvis", "settings.json")))
        deny_write.append(
            os.path.normpath(os.path.join(cwd, ".tabvis", "settings.local.json"))
        )

    # Block writes to .tabvis/skills in both original and current working directories.
    deny_write.append(os.path.normpath(os.path.join(original_cwd, ".tabvis", "skills")))
    if cwd != original_cwd:
        deny_write.append(os.path.normpath(os.path.join(cwd, ".tabvis", "skills")))

    # SECURITY: scrub planted bare-git-repo files (see scrub_bare_git_repo_files).
    _bare_git_repo_scrub_paths.clear()
    bare_git_repo_files = ["HEAD", "objects", "refs", "hooks", "config"]
    dirs_to_check = [original_cwd] if cwd == original_cwd else [original_cwd, cwd]
    for directory in dirs_to_check:
        for git_file in bare_git_repo_files:
            p = os.path.normpath(os.path.join(directory, git_file))
            try:
                os.stat(p)
                deny_write.append(p)
            except OSError:
                _bare_git_repo_scrub_paths.append(p)

    # Git worktree: main repo path needs write access for index.lock etc.
    if _worktree_main_repo_path is not None and _worktree_main_repo_path != cwd:
        allow_write.append(_worktree_main_repo_path)

    # --add-dir directories (persisted in settings + session-only bootstrap state).
    additional_dirs: list[str] = []
    seen_dirs: set[str] = set()
    for d in [
        *(_dig(settings, "permissions", "additionalDirectories") or []),
        *get_additional_directories_for_tabvis_md(),
    ]:
        if d not in seen_dirs:
            seen_dirs.add(d)
            additional_dirs.append(d)
    allow_write.extend(additional_dirs)

    # Iterate each settings source to resolve paths relative to the right settings dir.
    for source in SETTING_SOURCES:
        source_settings = get_settings_for_source(source)

        source_permissions = _dig(source_settings, "permissions")
        if source_permissions:
            for rule_string in source_permissions.get("allow") or []:
                rule = _permission_rule_value_from_string(rule_string)
                if rule.get("toolName") == FILE_EDIT_TOOL_NAME and rule.get("ruleContent"):
                    allow_write.append(
                        resolve_path_pattern_for_sandbox(rule["ruleContent"], source)
                    )

            for rule_string in source_permissions.get("deny") or []:
                rule = _permission_rule_value_from_string(rule_string)
                if rule.get("toolName") == FILE_EDIT_TOOL_NAME and rule.get("ruleContent"):
                    deny_write.append(
                        resolve_path_pattern_for_sandbox(rule["ruleContent"], source)
                    )
                if rule.get("toolName") == FILE_READ_TOOL_NAME and rule.get("ruleContent"):
                    deny_read.append(
                        resolve_path_pattern_for_sandbox(rule["ruleContent"], source)
                    )

        # sandbox.filesystem.* uses STANDARD path semantics (/path = absolute). #30067
        fs = _dig(source_settings, "sandbox", "filesystem")
        if fs:
            for p in fs.get("allowWrite") or []:
                allow_write.append(resolve_sandbox_filesystem_path(p, source))
            for p in fs.get("denyWrite") or []:
                deny_write.append(resolve_sandbox_filesystem_path(p, source))
            for p in fs.get("denyRead") or []:
                deny_read.append(resolve_sandbox_filesystem_path(p, source))
            if not _should_allow_managed_read_paths_only() or source == "policySettings":
                for p in fs.get("allowRead") or []:
                    allow_read.append(resolve_sandbox_filesystem_path(p, source))

    # Ripgrep config — user settings take priority; otherwise pass our rg.
    rg_path, rg_args = ripgrep_command()
    argv0 = None  # No embedded/argv0-dispatch ripgrep in this implementation.
    ripgrep_config = _dig(settings, "sandbox", "ripgrep")
    if ripgrep_config is None:
        ripgrep_config = {"command": rg_path, "args": rg_args, "argv0": argv0}

    return {
        "network": {
            "allowedDomains": allowed_domains,
            "deniedDomains": denied_domains,
            "allowUnixSockets": _dig(settings, "sandbox", "network", "allowUnixSockets"),
            "allowAllUnixSockets": _dig(
                settings, "sandbox", "network", "allowAllUnixSockets"
            ),
            "allowLocalBinding": _dig(settings, "sandbox", "network", "allowLocalBinding"),
            "httpProxyPort": _dig(settings, "sandbox", "network", "httpProxyPort"),
            "socksProxyPort": _dig(settings, "sandbox", "network", "socksProxyPort"),
        },
        "filesystem": {
            "denyRead": deny_read,
            "allowRead": allow_read,
            "allowWrite": allow_write,
            "denyWrite": deny_write,
        },
        "ignoreViolations": _dig(settings, "sandbox", "ignoreViolations"),
        "enableWeakerNestedSandbox": _dig(
            settings, "sandbox", "enableWeakerNestedSandbox"
        ),
        "enableWeakerNetworkIsolation": _dig(
            settings, "sandbox", "enableWeakerNetworkIsolation"
        ),
        "ripgrep": ripgrep_config,
    }


# ============================================================================
# Tabvis CLI-specific state
# ============================================================================

_initialization_promise: Awaitable[None] | None = None
_settings_subscription_cleanup: Callable[[], None] | None = None

# Cached main repo path for git worktrees, resolved once during initialize().
# None = not yet resolved OR not a worktree (we collapse the TS undefined/null distinction with a
# separate _worktree_resolved flag).
_worktree_main_repo_path: str | None = None
_worktree_resolved = False

# Bare-repo files at cwd that didn't exist at config time and should be scrubbed post-command.
_bare_git_repo_scrub_paths: list[str] = []

# Memoized values (lodash.memoize -> module-level cache cells).
_check_dependencies_cache: SandboxDependencyCheck | None = None
_is_supported_platform_cache: bool | None = None


def scrub_bare_git_repo_files() -> None:
    """Delete bare-repo files planted at cwd during a sandboxed command.

    See the SECURITY block in :func:`convert_to_sandbox_runtime_config`. ENOENT is the expected
    common case (nothing was planted).
    """
    import shutil

    for p in _bare_git_repo_scrub_paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            log_for_debugging(f"[Sandbox] scrubbed planted bare-repo file: {p}")
        except OSError:
            # ENOENT is the expected common case — nothing was planted.
            pass


_GITDIR_RE = re.compile(r"^gitdir:\s*(.+)$", re.MULTILINE)


async def detect_worktree_main_repo_path(cwd: str) -> str | None:
    """Detect if ``cwd`` is a git worktree and resolve the main repo path.

    In a worktree, ``.git`` is a *file* containing ``"gitdir: ..."``. If ``.git`` is a directory
    (or unreadable), returns ``None``.
    """
    git_path = os.path.join(cwd, ".git")
    try:
        with open(git_path, encoding="utf-8") as fh:
            git_content = fh.read()
    except (OSError, UnicodeDecodeError):
        # Not in a worktree, .git is a directory (EISDIR), or can't read .git file.
        return None

    gitdir_match = _GITDIR_RE.search(git_content)
    if not gitdir_match or not gitdir_match.group(1):
        return None

    # gitdir may be relative — resolve against cwd.
    gitdir = os.path.normpath(os.path.join(cwd, gitdir_match.group(1).strip()))
    # gitdir format: /path/to/main/repo/.git/worktrees/worktree-name
    # Match the /.git/worktrees/ segment specifically.
    sep = os.sep
    marker = f"{sep}.git{sep}worktrees{sep}"
    marker_index = gitdir.rfind(marker)
    if marker_index > 0:
        return gitdir[:marker_index]
    return None


def check_dependencies() -> SandboxDependencyCheck:
    """Check whether sandbox dependencies are available, memoizing the result.

    Returns ``{errors, warnings}`` — non-empty ``errors`` means the sandbox cannot run.
    """
    global _check_dependencies_cache
    if _check_dependencies_cache is not None:
        return _check_dependencies_cache
    rg_path, rg_args = ripgrep_command()
    _check_dependencies_cache = BaseSandboxManager.check_dependencies(
        {"command": rg_path, "args": rg_args}
    )
    return _check_dependencies_cache


def get_sandbox_enabled_setting() -> bool:
    """``settings.sandbox.enabled ?? false``."""
    try:
        settings = _get_settings_deprecated()
        value = _dig(settings, "sandbox", "enabled")
        return value if value is not None else False
    except Exception as error:  # noqa: BLE001 — never let a settings read crash the check.
        log_for_debugging(f"Failed to get settings for sandbox check: {error}")
        return False


def is_auto_allow_bash_if_sandboxed_enabled() -> bool:
    """Return ``settings.sandbox.autoAllowBashIfSandboxed``, defaulting to true."""
    settings = _get_settings_deprecated()
    value = _dig(settings, "sandbox", "autoAllowBashIfSandboxed")
    return value if value is not None else True


def are_unsandboxed_commands_allowed() -> bool:
    """Return ``settings.sandbox.allowUnsandboxedCommands``, defaulting to true."""
    settings = _get_settings_deprecated()
    value = _dig(settings, "sandbox", "allowUnsandboxedCommands")
    return value if value is not None else True


def is_sandbox_required() -> bool:
    """Require the sandbox when enabled and configured to fail if unavailable."""
    settings = _get_settings_deprecated()
    fail_if_unavailable = _dig(settings, "sandbox", "failIfUnavailable")
    return get_sandbox_enabled_setting() and (
        fail_if_unavailable if fail_if_unavailable is not None else False
    )


def is_supported_platform() -> bool:
    """Return whether the current platform supports sandboxing, memoizing the result."""
    global _is_supported_platform_cache
    if _is_supported_platform_cache is None:
        _is_supported_platform_cache = BaseSandboxManager.is_supported_platform()
    return _is_supported_platform_cache


def is_platform_in_enabled_list() -> bool:
    """Return whether the platform is allowed by ``sandbox.enabledPlatforms``.

    When ``enabledPlatforms`` is unset, all supported platforms are allowed. An empty list disables
    sandbox everywhere. On a read error, defaults to enabled.
    """
    try:
        settings = get_initial_settings().model_dump(by_alias=True, exclude_none=True)
        enabled_platforms: list[Platform] | None = _dig(
            settings, "sandbox", "enabledPlatforms"
        )

        if enabled_platforms is None:
            return True
        if len(enabled_platforms) == 0:
            return False
        return get_platform() in enabled_platforms
    except Exception as error:  # noqa: BLE001 — default to enabled if settings unreadable.
        log_for_debugging(f"Failed to check enabledPlatforms: {error}")
        return True  # Default to enabled if we can't read settings.


def is_sandboxing_enabled() -> bool:
    """Whether sandboxing is enabled.

    Checks platform support, dependency availability, the ``enabledPlatforms`` restriction, and the
    user's ``sandbox.enabled`` setting.
    """
    if not is_supported_platform():
        return False
    if len(check_dependencies().get("errors", [])) > 0:
        return False
    if not is_platform_in_enabled_list():
        return False
    return get_sandbox_enabled_setting()


def get_sandbox_unavailable_reason() -> str | None:
    """Return why an explicitly enabled sandbox cannot run, or ``None`` when available.

    Only warns when the user explicitly set ``sandbox.enabled``. Covers unsupported platform,
    ``enabledPlatforms`` exclusion, and missing dependencies.
    """
    if not get_sandbox_enabled_setting():
        return None

    if not is_supported_platform():
        platform = get_platform()
        if platform == "wsl":
            return "sandbox.enabled is set but WSL1 is not supported (requires WSL2)"
        return (
            f"sandbox.enabled is set but {platform} is not supported "
            "(requires macOS, Linux, or WSL2)"
        )

    if not is_platform_in_enabled_list():
        return (
            f"sandbox.enabled is set but {get_platform()} is not in sandbox.enabledPlatforms"
        )

    deps = check_dependencies()
    if len(deps.get("errors", [])) > 0:
        platform = get_platform()
        hint = (
            "run /sandbox or /doctor for details"
            if platform == "macos"
            else "install missing tools (e.g. apt install bubblewrap socat) or run /sandbox for details"
        )
        return (
            f"sandbox.enabled is set but dependencies are missing: "
            f"{', '.join(deps['errors'])} · {hint}"
        )

    return None


_HAS_GLOB_RE = re.compile(r"[*?[\]]")
_TRAILING_GLOB_RE = re.compile(r"/\*\*$")


def get_linux_glob_pattern_warnings() -> list[str]:
    """Permission rules whose globs won't work fully on Linux/WSL."""
    platform = get_platform()
    if platform not in ("linux", "wsl"):
        return []

    try:
        settings = _get_settings_deprecated()

        if not _dig(settings, "sandbox", "enabled"):
            return []

        permissions = settings.get("permissions") or {}
        warnings: list[str] = []

        def _has_globs(path: str) -> bool:
            stripped = _TRAILING_GLOB_RE.sub("", path)
            return _HAS_GLOB_RE.search(stripped) is not None

        for rule_string in [
            *(permissions.get("allow") or []),
            *(permissions.get("deny") or []),
        ]:
            rule = _permission_rule_value_from_string(rule_string)
            rule_content = rule.get("ruleContent")
            if (
                rule.get("toolName") in (FILE_EDIT_TOOL_NAME, FILE_READ_TOOL_NAME)
                and rule_content
                and _has_globs(rule_content)
            ):
                warnings.append(rule_string)

        return warnings
    except Exception as error:  # noqa: BLE001
        log_for_debugging(f"Failed to get Linux glob pattern warnings: {error}")
        return []


def are_sandbox_settings_locked_by_policy() -> bool:
    """Whether sandbox settings are locked by an overriding source.

    True if any of ``enabled`` / ``autoAllowBashIfSandboxed`` / ``allowUnsandboxedCommands`` is set
    in ``flagSettings`` or ``policySettings`` (both higher priority than ``localSettings``).
    """
    overriding_sources: tuple[SettingSource, ...] = ("flagSettings", "policySettings")
    for source in overriding_sources:
        settings = get_settings_for_source(source)
        sandbox = _dig(settings, "sandbox")
        if isinstance(sandbox, dict) and (
            sandbox.get("enabled") is not None
            or sandbox.get("autoAllowBashIfSandboxed") is not None
            or sandbox.get("allowUnsandboxedCommands") is not None
        ):
            return True
    return False


async def set_sandbox_settings(options: dict[str, Any]) -> None:
    """Set sandbox settings in ``localSettings``.

    ``options`` may carry ``enabled`` / ``autoAllowBashIfSandboxed`` / ``allowUnsandboxedCommands``
    (only the present keys are written).
    """
    existing_settings = get_settings_for_source("localSettings")
    existing_sandbox = dict(_dig(existing_settings, "sandbox") or {})

    new_sandbox = dict(existing_sandbox)
    if options.get("enabled") is not None:
        new_sandbox["enabled"] = options["enabled"]
    if options.get("autoAllowBashIfSandboxed") is not None:
        new_sandbox["autoAllowBashIfSandboxed"] = options["autoAllowBashIfSandboxed"]
    if options.get("allowUnsandboxedCommands") is not None:
        new_sandbox["allowUnsandboxedCommands"] = options["allowUnsandboxedCommands"]

    _update_settings_for_source("localSettings", {"sandbox": new_sandbox})


def get_excluded_commands() -> list[str]:
    """``settings.sandbox.excludedCommands ?? []``."""
    settings = _get_settings_deprecated()
    return _dig(settings, "sandbox", "excludedCommands") or []


async def wrap_with_sandbox(
    command: str,
    bin_shell: str | None = None,
    custom_config: dict[str, Any] | None = None,
    abort_signal: Any | None = None,
) -> str:
    """Wrap ``command`` with the sandbox.

    Ensures initialization is complete if sandboxing is enabled, then delegates to the base
    manager (a pass-through in this unbundled build).
    """
    if is_sandboxing_enabled():
        if _initialization_promise is not None:
            await _initialization_promise
        else:
            raise RuntimeError("Sandbox failed to initialize. ")

    return await BaseSandboxManager.wrap_with_sandbox(
        command, bin_shell, custom_config, abort_signal
    )


async def initialize(sandbox_ask_callback: SandboxAskCallback | None = None) -> None:
    """Initialize the sandbox with log monitoring.

    No-op if already initializing/initialized or sandboxing is disabled. Resolves the worktree
    main-repo path once, builds the runtime config, initializes the base manager, and subscribes to
    settings changes to refresh the config dynamically. Errors are logged (fail gracefully).
    """
    global _initialization_promise, _worktree_main_repo_path, _worktree_resolved
    global _settings_subscription_cleanup

    if _initialization_promise is not None:
        await _initialization_promise
        return

    if not is_sandboxing_enabled():
        return

    # Wrap the callback to enforce allowManagedDomainsOnly policy across all code paths.
    wrapped_callback: SandboxAskCallback | None
    if sandbox_ask_callback is not None:

        async def _wrapped(host_pattern: NetworkHostPattern) -> bool:
            if should_allow_managed_sandbox_domains_only():
                log_for_debugging(
                    f"[sandbox] Blocked network request to {host_pattern.get('host')} "
                    "(allowManagedDomainsOnly)"
                )
                return False
            return await sandbox_ask_callback(host_pattern)

        wrapped_callback = _wrapped
    else:
        wrapped_callback = None

    async def _run() -> None:
        global _initialization_promise, _worktree_main_repo_path, _worktree_resolved
        global _settings_subscription_cleanup
        try:
            # Resolve worktree main repo path once before building config.
            if not _worktree_resolved:
                _worktree_main_repo_path = await detect_worktree_main_repo_path(
                    get_cwd_state()
                )
                _worktree_resolved = True

            settings = _get_settings_deprecated()
            runtime_config = convert_to_sandbox_runtime_config(settings)

            await BaseSandboxManager.initialize(runtime_config, wrapped_callback)

            # Subscribe to settings changes to refresh the config dynamically.
            def _on_settings_change(*_args: Any) -> None:
                settings2 = _get_settings_deprecated()
                new_config = convert_to_sandbox_runtime_config(settings2)
                BaseSandboxManager.update_config(new_config)
                log_for_debugging("Sandbox configuration updated from settings change")

            _settings_subscription_cleanup = settings_change_detector["subscribe"](
                _on_settings_change
            )
        except Exception as error:  # noqa: BLE001 — fail gracefully.
            _initialization_promise = None
            log_for_debugging(f"Failed to initialize sandbox: {get_error_message(error)}")

    coro = _run()
    _initialization_promise = coro
    await coro


def refresh_config() -> None:
    """Refresh the sandbox config from current settings immediately."""
    if not is_sandboxing_enabled():
        return
    settings = _get_settings_deprecated()
    new_config = convert_to_sandbox_runtime_config(settings)
    BaseSandboxManager.update_config(new_config)


async def reset() -> None:
    """Reset sandbox state and clear memoized values."""
    global _settings_subscription_cleanup, _worktree_main_repo_path, _worktree_resolved
    global _initialization_promise

    if _settings_subscription_cleanup is not None:
        _settings_subscription_cleanup()
    _settings_subscription_cleanup = None
    _worktree_main_repo_path = None
    _worktree_resolved = False
    _bare_git_repo_scrub_paths.clear()

    _clear_memo_caches()
    _initialization_promise = None

    await BaseSandboxManager.reset()


def _clear_memo_caches() -> None:
    """Clear the lodash.memoize-equivalent module caches (``checkDependencies`` / ``isSupportedPlatform``)."""
    global _check_dependencies_cache, _is_supported_platform_cache
    _check_dependencies_cache = None
    _is_supported_platform_cache = None


def add_to_excluded_commands(
    command: str,
    permission_updates: list[dict[str, Any]] | None = None,
) -> str:
    """Add a command to the ``excludedCommands`` list.

    If ``permission_updates`` carries an ``addRules`` update with a Bash rule, the command pattern
    is extracted from it (e.g. ``"npm run test"`` from ``"npm run test:*"``); otherwise the exact
    command is used. Returns the pattern that was added.
    """
    existing_settings = get_settings_for_source("localSettings")
    existing_excluded_commands = _dig(existing_settings, "sandbox", "excludedCommands") or []

    command_pattern: str = command

    if permission_updates:
        bash_suggestions = [
            update
            for update in permission_updates
            if update.get("type") == "addRules"
            and any(
                rule.get("toolName") == BASH_TOOL_NAME
                for rule in update.get("rules", [])
            )
        ]

        if bash_suggestions and bash_suggestions[0].get("type") == "addRules":
            first_bash_rule = next(
                (
                    rule
                    for rule in bash_suggestions[0].get("rules", [])
                    if rule.get("toolName") == BASH_TOOL_NAME
                ),
                None,
            )
            if first_bash_rule and first_bash_rule.get("ruleContent"):
                prefix = _permission_rule_extract_prefix(first_bash_rule["ruleContent"])
                command_pattern = prefix or first_bash_rule["ruleContent"]

    if command_pattern not in existing_excluded_commands:
        new_sandbox = dict(_dig(existing_settings, "sandbox") or {})
        new_sandbox["excludedCommands"] = [*existing_excluded_commands, command_pattern]
        _update_settings_for_source("localSettings", {"sandbox": new_sandbox})

    return command_pattern


# ============================================================================
# Local settings writer (faithful copy of updateSettingsForSource for editable sources)
# ============================================================================


def _merge_with_array_replace(
    target: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Deep-merge ``source`` into a copy of ``target`` (TS ``mergeWith`` customizer semantics).

    - ``None`` source value -> delete the key.
    - list source value -> replace wholesale (caller owns the final state).
    - two dicts -> recurse.
    - otherwise -> source wins.
    """
    result = dict(target)
    for key, src_val in source.items():
        if src_val is None:
            result.pop(key, None)
            continue
        if isinstance(src_val, list):
            result[key] = src_val
            continue
        tgt_val = result.get(key)
        if isinstance(tgt_val, dict) and isinstance(src_val, dict):
            result[key] = _merge_with_array_replace(tgt_val, src_val)
            continue
        result[key] = src_val
    return result


def _update_settings_for_source(
    source: EditableSettingSource, settings: dict[str, Any]
) -> dict[str, Any | None]:
    """Update the settings for source.

    Policy / flag sources are no-ops. Creates parent dirs, deep-merges into the existing file
    (arrays replace, ``None`` deletes), writes JSON (two-space indent), and resets the session
    cache. Returns ``{"error": Exception | None}``.
    """
    if source in ("policySettings", "flagSettings"):
        return {"error": None}

    file_path = get_settings_file_path_for_source(source)
    if not file_path:
        return {"error": None}

    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        existing = get_settings_for_source(source) or {}
        updated = _merge_with_array_replace(existing, settings)
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(updated, indent=2) + "\n")
        reset_settings_cache()
    except OSError as exc:
        return {"error": exc}

    return {"error": None}


# ============================================================================
# Export interface and implementation — the Tabvis CLI SandboxManager aggregate.
# ============================================================================


class _SandboxManager:
    """Tabvis CLI sandbox manager — wraps the (unbundled) sandbox runtime with Tabvis-specific features.

    The custom methods carry the existing gating /
    rule logic; the ``get_*`` accessors forward to :data:`BaseSandboxManager` (all ``None`` in this
    unbundled build). Exposed as a singleton :data:`SandboxManager`.
    """

    # Custom implementations.
    initialize = staticmethod(initialize)
    is_sandboxing_enabled = staticmethod(is_sandboxing_enabled)
    is_sandbox_enabled_in_settings = staticmethod(get_sandbox_enabled_setting)
    is_platform_in_enabled_list = staticmethod(is_platform_in_enabled_list)
    get_sandbox_unavailable_reason = staticmethod(get_sandbox_unavailable_reason)
    is_auto_allow_bash_if_sandboxed_enabled = staticmethod(
        is_auto_allow_bash_if_sandboxed_enabled
    )
    are_unsandboxed_commands_allowed = staticmethod(are_unsandboxed_commands_allowed)
    is_sandbox_required = staticmethod(is_sandbox_required)
    are_sandbox_settings_locked_by_policy = staticmethod(
        are_sandbox_settings_locked_by_policy
    )
    set_sandbox_settings = staticmethod(set_sandbox_settings)
    get_excluded_commands = staticmethod(get_excluded_commands)
    wrap_with_sandbox = staticmethod(wrap_with_sandbox)
    refresh_config = staticmethod(refresh_config)
    reset = staticmethod(reset)
    check_dependencies = staticmethod(check_dependencies)

    # Forward to base sandbox manager.
    get_fs_read_config = staticmethod(BaseSandboxManager.get_fs_read_config)
    get_fs_write_config = staticmethod(BaseSandboxManager.get_fs_write_config)
    get_network_restriction_config = staticmethod(
        BaseSandboxManager.get_network_restriction_config
    )
    get_ignore_violations = staticmethod(BaseSandboxManager.get_ignore_violations)
    get_linux_glob_pattern_warnings = staticmethod(get_linux_glob_pattern_warnings)
    is_supported_platform = staticmethod(is_supported_platform)
    get_allow_unix_sockets = staticmethod(BaseSandboxManager.get_allow_unix_sockets)
    get_allow_local_binding = staticmethod(BaseSandboxManager.get_allow_local_binding)
    get_enable_weaker_nested_sandbox = staticmethod(
        BaseSandboxManager.get_enable_weaker_nested_sandbox
    )
    get_proxy_port = staticmethod(BaseSandboxManager.get_proxy_port)
    get_socks_proxy_port = staticmethod(BaseSandboxManager.get_socks_proxy_port)
    get_linux_http_socket_path = staticmethod(
        BaseSandboxManager.get_linux_http_socket_path
    )
    get_linux_socks_socket_path = staticmethod(
        BaseSandboxManager.get_linux_socks_socket_path
    )
    wait_for_network_initialization = staticmethod(
        BaseSandboxManager.wait_for_network_initialization
    )
    get_sandbox_violation_store = staticmethod(
        BaseSandboxManager.get_sandbox_violation_store
    )
    annotate_stderr_with_sandbox_failures = staticmethod(
        BaseSandboxManager.annotate_stderr_with_sandbox_failures
    )

    @staticmethod
    def cleanup_after_command() -> None:
        """Cleanup after a command: base cleanup, then scrub planted bare-repo files."""
        BaseSandboxManager.cleanup_after_command()
        scrub_bare_git_repo_files()


SandboxManager = _SandboxManager
