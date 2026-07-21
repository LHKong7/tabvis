"""Built-in plugin adapters (design §8.7).

The migration path: register today's extension mechanisms as built-in plugins so they share the
registry's discovery/validation/lifecycle/permissions — without changing how each actually executes
(design §8.1, §8.7). Order of §8.7: browser engines → MCP tool providers → Skill context providers →
channels → (later) third-party installation.

These adapters are thin: each declares a manifest and a trivial lifecycle. Deep-wiring an adapter to
its subsystem (an MCP server's real tools, a Skill's real prompts) is follow-up work; what Phase 6
proves is that heterogeneous mechanisms validate and run under one registry.
"""

from __future__ import annotations

from tabvis import __version__ as TABVIS_VERSION
from tabvis.gateway.plugins.contract import Plugin, PluginCandidate, PluginHealth
from tabvis.gateway.plugins.manifest import (
    KIND_BROWSER_ENGINE,
    KIND_CHANNEL,
    KIND_CONTEXT_PROVIDER,
    KIND_TOOL_PROVIDER,
    PluginManifest,
    PluginRequirements,
)

# A constraint that always accepts the running host — the built-ins ship with tabvis itself.
_HOST_OK = ">=0.0"


class BuiltinPlugin:
    """A minimal Plugin: tracks started/stopped and reports health. Base for the built-in adapters."""

    def __init__(self, manifest: PluginManifest) -> None:
        self.manifest = manifest
        self._started = False

    async def start(self, services: object) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def health(self) -> PluginHealth:
        return PluginHealth(status="ready" if self._started else "stopped")


def _candidate(manifest: PluginManifest, factory) -> PluginCandidate:
    return PluginCandidate(manifest=manifest, source="builtin", factory=factory)


def browser_engine_plugin(engine: str = "chromium") -> PluginCandidate:
    manifest = PluginManifest(
        id=f"engine.{engine}", version="1.0.0", kind=KIND_BROWSER_ENGINE,
        capabilities=("browser.drive",), permissions=("browser:launch",),
        requires=PluginRequirements(tabvis=_HOST_OK), optional=False,  # a browser engine is core
    )
    return _candidate(manifest, lambda: BuiltinPlugin(manifest))


def mcp_tool_provider_plugin(server: str = "filesystem") -> PluginCandidate:
    manifest = PluginManifest(
        id=f"mcp.{server}", version="1.0.0", kind=KIND_TOOL_PROVIDER,
        capabilities=("tool.provide",), permissions=("tool:invoke",),
        requires=PluginRequirements(tabvis=_HOST_OK),
    )
    return _candidate(manifest, lambda: BuiltinPlugin(manifest))


def skill_context_provider_plugin(skill: str = "deep-research") -> PluginCandidate:
    manifest = PluginManifest(
        id=f"skill.{skill}", version="1.0.0", kind=KIND_CONTEXT_PROVIDER,
        capabilities=("context.provide",), permissions=(),
        requires=PluginRequirements(tabvis=_HOST_OK),
    )
    return _candidate(manifest, lambda: BuiltinPlugin(manifest))


def channel_plugin_candidate(channel) -> PluginCandidate:
    """Wrap a `channels` plugin as a registry plugin (design §8.7 step 4)."""
    cm = channel.manifest
    manifest = PluginManifest(
        id=f"channel.{cm.plugin_id}", version=cm.version, kind=KIND_CHANNEL,
        capabilities=tuple(sorted(cm.capabilities)), permissions=("gateway:conversation.submit",),
        requires=PluginRequirements(tabvis=_HOST_OK),
    )

    class _ChannelAdapter(BuiltinPlugin):
        def __init__(self) -> None:
            super().__init__(manifest)
            self.channel = channel

    return _candidate(manifest, _ChannelAdapter)


def builtin_candidates() -> list[PluginCandidate]:
    """The standard built-in set, in §8.7 registration order."""
    from tabvis.channels.web.channel import WebChannel

    return [
        browser_engine_plugin("chromium"),
        mcp_tool_provider_plugin("filesystem"),
        skill_context_provider_plugin("deep-research"),
        channel_plugin_candidate(WebChannel()),
    ]
