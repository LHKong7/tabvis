"""MCP types.

Config schemas (loaded from ``.mcp.json`` / settings ``mcpServers``) + runtime connection states
+ resource/serialized-state types. These are pydantic v2 models; the discriminated union of config
types is resolved by :func:`parse_mcp_server_config`. The ``mcp`` SDK ``ClientSession`` is typed
loosely (``Any``) here so this module has no hard ``mcp`` import — the connection layer wires the
real session.

Casing: Python attrs are snake_case; the camelCase wire keys (``headersHelper``, ``mcpServers``,
``inputJSONSchema``, ``mimeType`` …) are pydantic ``alias``es with ``populate_by_name=True``, so
loading from JSON and ``model_dump(by_alias=True)`` both round-trip the wire form.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ConfigScope: where a server config came from.
ConfigScope = Literal["local", "user", "project", "dynamic", "enterprise", "managed"]

# Transport types Tabvis understands.
Transport = Literal["stdio", "sse", "sse-ide", "http", "ws", "sdk"]

_WIRE = ConfigDict(extra="forbid", populate_by_name=True)


class McpStdioServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["stdio"] | None = None  # optional for backwards compatibility
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None


class McpSSEServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["sse"]
    url: str
    headers: dict[str, str] | None = None
    headers_helper: str | None = Field(default=None, alias="headersHelper")


class McpHTTPServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["http"]
    url: str
    headers: dict[str, str] | None = None
    headers_helper: str | None = Field(default=None, alias="headersHelper")


class McpWebSocketServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["ws"]
    url: str
    headers: dict[str, str] | None = None
    headers_helper: str | None = Field(default=None, alias="headersHelper")


class McpSdkServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["sdk"]
    name: str


# IDE-only variants (not used by the headless runtime).
class McpSSEIDEServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["sse-ide"]
    url: str
    ide_name: str = Field(alias="ideName")
    ide_running_in_windows: bool | None = Field(default=None, alias="ideRunningInWindows")


class McpWebSocketIDEServerConfig(BaseModel):
    model_config = _WIRE
    type: Literal["ws-ide"]
    url: str
    ide_name: str = Field(alias="ideName")
    auth_token: str | None = Field(default=None, alias="authToken")
    ide_running_in_windows: bool | None = Field(default=None, alias="ideRunningInWindows")


McpServerConfig = (
    McpStdioServerConfig
    | McpSSEServerConfig
    | McpSSEIDEServerConfig
    | McpWebSocketIDEServerConfig
    | McpHTTPServerConfig
    | McpWebSocketServerConfig
    | McpSdkServerConfig
)

_CONFIG_BY_TYPE: dict[str, type[BaseModel]] = {
    "stdio": McpStdioServerConfig,
    "sse": McpSSEServerConfig,
    "sse-ide": McpSSEIDEServerConfig,
    "ws-ide": McpWebSocketIDEServerConfig,
    "http": McpHTTPServerConfig,
    "ws": McpWebSocketServerConfig,
    "sdk": McpSdkServerConfig,
}


def parse_mcp_server_config(raw: dict[str, Any]) -> McpServerConfig:
    """Validate a raw server config dict into the right model (default type ``stdio``)."""
    server_type = raw.get("type") or "stdio"
    model = _CONFIG_BY_TYPE.get(server_type, McpStdioServerConfig)
    return model.model_validate(raw)


class McpJsonConfig(BaseModel):
    model_config = _WIRE
    mcp_servers: dict[str, dict[str, Any]] = Field(alias="mcpServers")

    @field_validator("mcp_servers")
    @classmethod
    def _validate_servers(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for raw in value.values():
            parse_mcp_server_config(raw)  # raises on invalid
        return value


@dataclass
class ScopedMcpServerConfig:
    """A server config tagged with the scope it was loaded from."""

    config: McpServerConfig
    scope: ConfigScope


# --- Runtime connection states ---


@dataclass
class ConnectedMCPServer:
    client: Any  # mcp.ClientSession — typed Any until the client wave
    name: str
    capabilities: dict[str, Any]
    config: ScopedMcpServerConfig
    cleanup: Any  # async () -> None
    server_info: dict[str, Any] | None = None
    instructions: str | None = None
    type: Literal["connected"] = "connected"


@dataclass
class FailedMCPServer:
    name: str
    config: ScopedMcpServerConfig
    error: str | None = None
    type: Literal["failed"] = "failed"


@dataclass
class DisabledMCPServer:
    name: str
    config: ScopedMcpServerConfig
    type: Literal["disabled"] = "disabled"


MCPServerConnection = (
    ConnectedMCPServer
    | FailedMCPServer
    | DisabledMCPServer
)


# --- Resource + serialized state types ---


class ServerResource(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)  # MCP Resource + 'server' tag
    server: str
    uri: str
    name: str | None = None
    description: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")


class SerializedTool(BaseModel):
    model_config = _WIRE
    name: str
    description: str
    input_json_schema: dict[str, Any] | None = Field(default=None, alias="inputJSONSchema")
    is_mcp: bool | None = Field(default=None, alias="isMcp")
    original_tool_name: str | None = Field(default=None, alias="originalToolName")


class SerializedClient(BaseModel):
    model_config = _WIRE
    name: str
    type: Literal["connected", "failed", "disabled"]
    capabilities: dict[str, Any] | None = None
