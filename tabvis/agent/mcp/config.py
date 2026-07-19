"""MCP config loader.

Assembles the server configs the agent will connect to. Covers only the *read* path:

* :func:`get_tabvis_mcp_configs` — merge configs by precedence ``user < project < dynamic`` and
  return ``{name: ScopedMcpServerConfig}`` (``{}`` when nothing is configured).
* project ``.mcp.json`` (scope ``"project"``) — read from :func:`tabvis.utils.cwd.get_cwd`, walking
  from filesystem root down to the cwd so closer files win.
* dynamic configs (scope ``"dynamic"``) — the ``TABVIS_MCP_CONFIG`` env var (a JSON document *or* a
  path to one) plus any ``dynamic_mcp_config`` the caller passes (later sources override earlier).
* user ``mcpServers`` (scope ``"user"``) — read from the ``~/.tabvis.json`` global config file when
  trivially present (``mcpServers`` key). The full settings subsystem is not yet implemented.
* ``${VAR}`` / ``${VAR:-default}`` expansion in config string values.

Validation goes through :class:`tabvis.agent.mcp.types.McpJsonConfig` /
:func:`tabvis.agent.mcp.types.parse_mcp_server_config`; no new models are introduced.

Not supported: enterprise/managed MCP config + exclusive control, allow/deny policy lists,
``local`` scope (project ``.tabvis.json`` mcpServers), project-approval gating, Windows ``npx``
cmd-wrapper warning, the write/add/remove/toggle surface, and settings-source gating — none are
exercised by this build's read path.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pydantic import ValidationError

from tabvis.agent.mcp.types import (
    ConfigScope,
    McpJsonConfig,
    McpServerConfig,
    ScopedMcpServerConfig,
    parse_mcp_server_config,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir
from tabvis.utils.errors import get_errno_code

# ----------------------------------------------------------------------------------------------
# Env-var expansion
# ----------------------------------------------------------------------------------------------

# Matches ${...} where the body has no closing brace.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def expand_env_vars_in_string(value: str) -> tuple[str, list[str]]:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in *value*.

    Returns ``(expanded, missing_vars)``. An unset variable with no default is left as the literal
    ``${VAR}`` (so it stays debuggable) and its name is collected in ``missing_vars``.
    """
    missing_vars: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        var_content = match.group(1)
        # Split on ":-" into at most 2 parts so a default value may itself contain ":-".
        parts = var_content.split(":-", 1)
        var_name = parts[0]
        default_value = parts[1] if len(parts) > 1 else None

        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default_value is not None:
            return default_value

        missing_vars.append(var_name)
        return match.group(0)  # original ${...}

    expanded = _ENV_VAR_PATTERN.sub(_replace, value)
    return expanded, missing_vars


def _expand_in_value(value: Any, missing: list[str]) -> Any:
    """Recursively expand env vars in strings within *value* (str / list / dict), in place by copy.

    A structural recursive walk over the raw dict covers the known config fields per server type
    (command/args/env, url/headers) without special-casing any of them.
    """
    if isinstance(value, str):
        expanded, vars_found = expand_env_vars_in_string(value)
        missing.extend(vars_found)
        return expanded
    if isinstance(value, list):
        return [_expand_in_value(item, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand_in_value(item, missing) for key, item in value.items()}
    return value


def expand_env_vars(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Expand env vars across a single raw server-config dict.

    IDE / ``sdk`` configs pass through effectively unchanged: the recursive walk here only touches
    strings, and those config types have no expandable user-supplied command/url. Returns
    ``(expanded, missing)`` with ``missing`` de-duplicated, preserving first-seen order.
    """
    missing: list[str] = []
    expanded = _expand_in_value(config, missing)
    deduped = list(dict.fromkeys(missing))
    return expanded, deduped


# ----------------------------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------------------------


def _safe_parse_json(text: str) -> Any | None:
    """Parse JSON, returning ``None`` on any failure."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def parse_mcp_config(
    config_object: Any,
    *,
    expand_vars: bool,
    scope: ConfigScope,
) -> dict[str, McpServerConfig]:
    """Validate a raw config object into ``{name: McpServerConfig}``.

    The top-level shape is validated by :class:`McpJsonConfig` (requires an ``mcpServers`` map);
    each entry is then parsed by :func:`parse_mcp_server_config`. On a schema failure this returns
    ``{}`` (errors are surfaced via debug logging rather than as rich ``ValidationError`` records).
    When ``expand_vars`` is set, ``${VAR}`` placeholders in each server's values are expanded
    before per-server validation.
    """
    try:
        json_config = McpJsonConfig.model_validate(config_object)
    except ValidationError:
        log_for_debugging(
            f"MCP config does not adhere to schema (scope={scope})",
        )
        return {}

    validated: dict[str, McpServerConfig] = {}
    for name, raw in json_config.mcp_servers.items():
        config_to_check = raw
        if expand_vars:
            expanded, missing = expand_env_vars(raw)
            if missing:
                log_for_debugging(
                    f"MCP server {name} (scope={scope}) has missing environment "
                    f"variables: {', '.join(missing)}",
                )
            config_to_check = expanded
        try:
            validated[name] = parse_mcp_server_config(config_to_check)
        except ValidationError:
            log_for_debugging(
                f"MCP server {name} (scope={scope}) does not adhere to schema",
            )
    return validated


def parse_mcp_config_from_file_path(
    file_path: str,
    *,
    expand_vars: bool,
    scope: ConfigScope,
) -> dict[str, McpServerConfig] | None:
    """Read and parse an ``.mcp.json``-shaped file.

    Returns ``None`` when the file is missing (expected for most directories) or
    unreadable/invalid; otherwise the validated ``{name: McpServerConfig}`` map.
    """
    try:
        with open(file_path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError as error:
        if get_errno_code(error) == "ENOENT":
            return None  # missing file is expected
        log_for_debugging(
            f"MCP config read error for {file_path} (scope={scope}): {error}",
        )
        return None

    parsed = _safe_parse_json(content)
    if parsed is None:
        log_for_debugging(
            f"MCP config is not valid JSON: {file_path} (scope={scope})",
        )
        return None

    return parse_mcp_config(parsed, expand_vars=expand_vars, scope=scope)


# ----------------------------------------------------------------------------------------------
# Scope loaders
# ----------------------------------------------------------------------------------------------


def _add_scope(
    servers: dict[str, McpServerConfig] | None,
    scope: ConfigScope,
) -> dict[str, ScopedMcpServerConfig]:
    """Tag each config with its scope."""
    if not servers:
        return {}
    return {name: ScopedMcpServerConfig(config=cfg, scope=scope) for name, cfg in servers.items()}


def get_project_mcp_configs() -> dict[str, ScopedMcpServerConfig]:
    """Load project ``.mcp.json`` servers (scope ``"project"``).

    Walks from the filesystem root down to the cwd, merging each directory's ``.mcp.json`` so
    files closer to the cwd override parents.
    """
    cwd = get_cwd()

    # Build the directory chain from cwd up to (but excluding) the filesystem root.
    dirs: list[str] = []
    current = cwd
    while current != os.path.dirname(current):  # dirname(root) == root
        dirs.append(current)
        current = os.path.dirname(current)

    all_servers: dict[str, ScopedMcpServerConfig] = {}
    # Root downward to cwd: later (closer) writes win.
    for directory in reversed(dirs):
        mcp_json_path = os.path.join(directory, ".mcp.json")
        servers = parse_mcp_config_from_file_path(
            mcp_json_path, expand_vars=True, scope="project"
        )
        if servers:
            all_servers.update(_add_scope(servers, "project"))
    return all_servers


def get_user_mcp_configs() -> dict[str, ScopedMcpServerConfig]:
    """Load user ``mcpServers`` for scope ``"user"``.

    Reads the ``mcpServers`` map from the merged settings (``settings.mcp_servers``) and from the
    ``~/.tabvis.json`` global config, then parses the combined map. With no settings and no global
    ``mcpServers`` key this returns ``{}``. Config only — no connections are opened here.

    Settings-source gating is not implemented in this build.
    """
    from tabvis.utils.settings.settings import get_initial_settings

    combined: dict[str, Any] = {}

    settings_servers = get_initial_settings().mcp_servers
    if settings_servers:
        combined.update(settings_servers)

    global_config_path = os.path.join(get_tabvis_config_home_dir(), ".tabvis.json")
    try:
        with open(global_config_path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        content = None

    if content is not None:
        parsed = _safe_parse_json(content)
        if isinstance(parsed, dict):
            global_servers = parsed.get("mcpServers")
            if global_servers:
                combined.update(global_servers)

    if not combined:
        return {}

    servers = parse_mcp_config({"mcpServers": combined}, expand_vars=True, scope="user")
    return _add_scope(servers, "user")


def get_dynamic_mcp_configs() -> dict[str, ScopedMcpServerConfig]:
    """Load dynamic configs from the ``TABVIS_MCP_CONFIG`` env var (scope ``"dynamic"``).

    The value is first tried as a JSON document, then as a path to one. Returns ``{}`` when
    unset/empty/invalid.
    """
    raw = os.environ.get("TABVIS_MCP_CONFIG")
    if not raw or not raw.strip():
        return {}
    raw = raw.strip()

    # First try to parse as a JSON string, then fall back to a file path.
    parsed_json = _safe_parse_json(raw)
    if parsed_json is not None:
        servers = parse_mcp_config(parsed_json, expand_vars=True, scope="dynamic")
    else:
        servers = (
            parse_mcp_config_from_file_path(
                os.path.abspath(raw), expand_vars=True, scope="dynamic"
            )
            or {}
        )
    return _add_scope(servers, "dynamic")


# ----------------------------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------------------------


def get_tabvis_mcp_configs(
    dynamic_mcp_config: dict[str, ScopedMcpServerConfig] | None = None,
) -> dict[str, ScopedMcpServerConfig]:
    """Assemble all MCP server configs for the headless session.

    Merges sources in order of precedence (later wins): ``user`` < ``project`` < dynamic
    (``TABVIS_MCP_CONFIG`` env, then the caller-supplied ``dynamic_mcp_config`` on top). Returns
    ``{}`` when nothing is configured.

    Not supported: enterprise exclusive-control short-circuit, allow/deny policy filtering,
    project-approval gating, and the ``local`` scope — none are reachable in this build, which has
    no managed settings / approval state.
    """
    merged: dict[str, ScopedMcpServerConfig] = {}
    merged.update(get_user_mcp_configs())
    merged.update(get_project_mcp_configs())
    merged.update(get_dynamic_mcp_configs())
    if dynamic_mcp_config:
        merged.update(dynamic_mcp_config)
    return merged
