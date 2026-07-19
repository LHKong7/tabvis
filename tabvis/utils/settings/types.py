"""Settings file schema

The Zod ``SettingsSchema`` (a ``.passthrough()`` object with ~50 optional keys) becomes a pydantic
v2 model that is intentionally **loose**: ``extra="allow"`` so unknown / not-yet-implemented keys are
preserved, ``populate_by_name=True`` so both the camelCase wire key (via ``alias``) and the
snake_case Python attribute construct/round-trip.

Only the keys the headless spine reads are given explicit fields (model, language, output style,
hooks, MCP servers, permissions, thinking/auto-memory/model-override toggles). Everything else flows
through ``extra``. Validation of the full schema (permission-rule shapes, MCP allow/deny entries,
sandbox, etc.) is not implemented in this build.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A single hook command, e.g. {"type": "command", "command": "...", "timeout": 5}.
# Loose dict — the full discriminated union (command/prompt/agent/http hooks) is deferred.
HookCommand = dict[str, Any]

# A hook matcher: {"matcher"?: str, "hooks": [HookCommand, ...]}.
HookMatcher = dict[str, Any]

# HooksSettings: event name (PreToolUse, PostToolUse, ...) -> list of matchers.
# Kept as a plain dict so unknown/not-yet-handled events pass through untouched.
HooksSettings = dict[str, list[HookMatcher]]


class PermissionsSettings(BaseModel):
    """``permissions`` section — allow / deny / ask rule lists (+ passthrough extras).

    ``rules`` carries the structured policy-engine rules (``{id, effect, actions, resources}``) from
    ``docs/permission-policy-engine_v1.md`` §5.3, distinct from the legacy ``allow/deny/ask`` string
    lists. Kept as raw dicts here; compiled + validated by ``tabvis.policy`` (PP-2), where an
    invalid rule is a startup error rather than being silently dropped.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    allow: list[str] | None = None
    deny: list[str] | None = None
    ask: list[str] | None = None
    rules: list[dict[str, Any]] | None = None
    mode: str | None = None


class SettingsJson(BaseModel):
    """A parsed settings file (``.tabvis/settings.json`` and friends).

    Loose by design: ``extra="allow"`` preserves the many keys this skeleton does not model, and
    ``populate_by_name=True`` lets either the camelCase wire alias or the snake_case attribute be
    used. Mirrors the Zod ``.passthrough()`` contract.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    model: str | None = None
    language: str | None = None
    output_style: str | None = Field(default=None, alias="outputStyle")
    hooks: HooksSettings | None = None
    mcp_servers: dict[str, Any] | None = Field(default=None, alias="mcpServers")
    permissions: PermissionsSettings | None = None
    always_thinking_enabled: bool | None = Field(default=None, alias="alwaysThinkingEnabled")
    auto_memory_enabled: bool | None = Field(default=None, alias="autoMemoryEnabled")
    auto_memory_directory: str | None = Field(default=None, alias="autoMemoryDirectory")
    model_overrides: dict[str, str] | None = Field(default=None, alias="modelOverrides")

    # --- browser agent (Playwright launch_persistent_context) --------------------------------
    # Read via tabvis.utils.browser_config accessors (env var > these fields > hardcoded default).
    browser_engine: str | None = Field(default=None, alias="browserEngine")
    browser_headless: bool | None = Field(default=None, alias="browserHeadless")
    browser_user_data_dir: str | None = Field(default=None, alias="browserUserDataDir")
    browser_viewport: dict[str, int] | None = Field(default=None, alias="browserViewport")
    browser_channel: str | None = Field(default=None, alias="browserChannel")
    browser_executable_path: str | None = Field(default=None, alias="browserExecutablePath")
    browser_timeout_ms: int | None = Field(default=None, alias="browserTimeoutMs")
    browser_allowed_domains: list[str] | None = Field(
        default=None, alias="browserAllowedDomains"
    )
    # Remote-attach endpoints for the connect/cdp engines (Browserbase/Browserless/Steel/anti-detect
    # browsers). Only the one matching the engine's mode is consulted. A ws endpoint may embed a
    # ``?token=`` — it is redacted before being logged or served, same as the proxy URL.
    browser_cdp_endpoint: str | None = Field(default=None, alias="browserCdpEndpoint")
    browser_ws_endpoint: str | None = Field(default=None, alias="browserWsEndpoint")

    # --- browser agent, cloak engine only (TABVIS_BROWSER_ENGINE=cloak) -------------------------
    # The CloakBrowser Pro license key is deliberately NOT here: it is a credential, and
    # settings.json is plain config that the console reads back. It is env-only
    # (TABVIS_BROWSER_CLOAK_LICENSE_KEY) — see tabvis.utils.browser_config.get_cloak_license_key.
    browser_proxy: str | None = Field(default=None, alias="browserProxy")
    browser_humanize: bool | None = Field(default=None, alias="browserHumanize")
    browser_human_preset: str | None = Field(default=None, alias="browserHumanPreset")
    browser_geoip: bool | None = Field(default=None, alias="browserGeoip")
    browser_timezone: str | None = Field(default=None, alias="browserTimezone")
    browser_locale: str | None = Field(default=None, alias="browserLocale")
    browser_cloak_version: str | None = Field(default=None, alias="browserCloakVersion")
