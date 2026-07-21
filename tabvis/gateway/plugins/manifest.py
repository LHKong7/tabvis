"""Plugin manifest (design §8.2).

The manifest is the static, declarative description every plugin ships: its id, version, kind,
entrypoint, the capabilities it provides, the permissions it *requests* (a maximum, not a grant —
design §8.6), and its requirements (host version + plugin dependencies). Parsing is strict: a
structurally invalid manifest is rejected at :func:`PluginManifest.from_dict`, before any validation
policy runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from tabvis.gateway.protocol.errors import GatewayError

SCHEMA_VERSION: Final = 1

# Plugin kinds (design §8.1). The runtime treats them uniformly; the kind selects the built-in adapter.
KIND_CHANNEL: Final = "channel"
KIND_TOOL_PROVIDER: Final = "tool_provider"       # e.g. an MCP server
KIND_CONTEXT_PROVIDER: Final = "context_provider"  # e.g. a Skill
KIND_BROWSER_ENGINE: Final = "browser_engine"
KIND_HOOK: Final = "hook"

KNOWN_KINDS: Final[frozenset[str]] = frozenset(
    {KIND_CHANNEL, KIND_TOOL_PROVIDER, KIND_CONTEXT_PROVIDER, KIND_BROWSER_ENGINE, KIND_HOOK}
)


@dataclass
class PluginRequirements:
    tabvis: str = "*"                       # host version constraint, e.g. ">=0.4,<0.6"
    plugins: tuple[str, ...] = ()           # plugin ids this one depends on
    capabilities: tuple[str, ...] = ()      # host/other-plugin capabilities this one needs

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PluginRequirements":
        data = data or {}
        return cls(
            tabvis=str(data.get("tabvis", "*")),
            plugins=tuple(data.get("plugins", []) or ()),
            capabilities=tuple(data.get("capabilities", []) or ()),
        )


@dataclass
class PluginManifest:
    id: str
    version: str
    kind: str
    entrypoint: str = ""
    capabilities: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    config_schema: str | None = None
    requires: PluginRequirements = field(default_factory=PluginRequirements)
    optional: bool = True                   # optional plugin failure only degrades its feature (§8.5)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        if not isinstance(data, dict):
            raise GatewayError("VALIDATION_FAILED", message="Manifest must be an object")
        for required in ("id", "version", "kind"):
            if not data.get(required):
                raise GatewayError("VALIDATION_FAILED", message=f"Manifest '{required}' is required")
        return cls(
            id=str(data["id"]),
            version=str(data["version"]),
            kind=str(data["kind"]),
            entrypoint=str(data.get("entrypoint", "")),
            capabilities=tuple(data.get("capabilities", []) or ()),
            permissions=tuple(data.get("permissions", []) or ()),
            config_schema=data.get("config_schema"),
            requires=PluginRequirements.from_dict(data.get("requires")),
            optional=bool(data.get("optional", True)),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "kind": self.kind,
            "entrypoint": self.entrypoint,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "config_schema": self.config_schema,
            "requires": {
                "tabvis": self.requires.tabvis,
                "plugins": list(self.requires.plugins),
                "capabilities": list(self.requires.capabilities),
            },
            "optional": self.optional,
        }
