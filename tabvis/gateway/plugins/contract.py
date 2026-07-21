"""Plugin contract types and lifecycle states (design §8.3, §8.4).

A :class:`Plugin` is any extension the registry drives — channel, tool provider, context provider,
browser engine, hook. It exposes only lifecycle (start/stop/health); what it *does* is its own concern,
reached through the capability-scoped services it is handed (design §8.6: no direct store access).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from tabvis.gateway.plugins.manifest import PluginManifest

# lifecycle states (design §8.3)
DISCOVERED: Final = "discovered"
VALIDATED: Final = "validated"
LOADED: Final = "loaded"
STARTED: Final = "started"
READY: Final = "ready"
DEGRADED: Final = "degraded"
STOPPING: Final = "stopping"
STOPPED: Final = "stopped"
REJECTED: Final = "rejected"


@dataclass
class PluginHealth:
    status: str  # ready | degraded | stopped
    detail: str | None = None


@dataclass
class PluginCandidate:
    """A discovered plugin, not yet validated (design §8.4)."""

    manifest: PluginManifest
    source: str = "builtin"          # where it came from (builtin | a directory path)
    factory: Any = None              # optional zero-arg callable returning a Plugin instance


@dataclass
class ValidationReport:
    """The outcome of validating a candidate (design §8.4)."""

    plugin_id: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    effective_permissions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


class Plugin(Protocol):
    manifest: PluginManifest

    async def start(self, services: Any) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> PluginHealth: ...
