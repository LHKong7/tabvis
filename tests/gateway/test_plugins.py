"""Phase 6 — Plugin Runtime: manifest, validation, dependencies, lifecycle (design §8, §15)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.gateway.plugins import builtin, version as version_mod
from tabvis.gateway.plugins.contract import DEGRADED, READY, REJECTED, PluginCandidate, PluginHealth
from tabvis.gateway.plugins.dependency import topological_order
from tabvis.gateway.plugins.manifest import KIND_TOOL_PROVIDER, PluginManifest, PluginRequirements
from tabvis.gateway.plugins.permissions import PermissionPolicy
from tabvis.gateway.plugins.registry import PluginRegistry
from tabvis.gateway.protocol.errors import GatewayError


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_parse_and_reject_missing_fields() -> None:
    m = PluginManifest.from_dict(
        {"id": "x", "version": "1.0.0", "kind": "channel", "capabilities": ["message.text.inbound"]}
    )
    assert m.id == "x" and m.kind == "channel" and "message.text.inbound" in m.capabilities
    with pytest.raises(GatewayError):
        PluginManifest.from_dict({"version": "1.0.0", "kind": "channel"})  # missing id


# --- version constraints -----------------------------------------------------------------------


def test_version_satisfies() -> None:
    assert version_mod.satisfies("0.0.1", ">=0.0")
    assert version_mod.satisfies("0.5.0", ">=0.4,<0.6")
    assert not version_mod.satisfies("0.0.1", ">=0.4,<0.6")  # the design's example vs host 0.0.1
    assert version_mod.satisfies("1.2.3", "*")


# --- permissions -------------------------------------------------------------------------------


def test_permission_policy_effective_and_over_privileged() -> None:
    policy = PermissionPolicy(grantable=("gateway:*", "secret:feishu.*:read"), granted=("gateway:*",))
    requested = ("gateway:conversation.submit", "secret:feishu.abc:read")
    assert policy.over_privileged(requested) == []                 # both within the ceiling
    assert policy.effective(requested) == ("gateway:conversation.submit",)  # only gateway:* granted
    # a request outside the ceiling is over-privileged.
    assert policy.over_privileged(("fs:/etc/passwd:read",)) == ["fs:/etc/passwd:read"]


# --- dependency graph --------------------------------------------------------------------------


def test_topological_order_and_cycle_detection() -> None:
    order = topological_order({"a": (), "b": ("a",), "c": ("b",)})
    assert order.index("a") < order.index("b") < order.index("c")
    with pytest.raises(GatewayError) as ei:
        topological_order({"a": ("b",), "b": ("a",)})
    assert "cycle" in ei.value.message.lower()
    with pytest.raises(GatewayError):
        topological_order({"a": ("missing",)})


# --- validation: reject incompatible / over-privileged before startup --------------------------


def _candidate(**overrides) -> PluginCandidate:
    manifest = PluginManifest(
        id=overrides.get("id", "p"), version="1.0.0", kind=overrides.get("kind", KIND_TOOL_PROVIDER),
        capabilities=overrides.get("capabilities", ()),
        permissions=overrides.get("permissions", ()),
        requires=overrides.get("requires", PluginRequirements(tabvis=">=0.0")),
        optional=overrides.get("optional", True),
    )
    return PluginCandidate(manifest=manifest, factory=lambda: builtin.BuiltinPlugin(manifest))


def test_incompatible_plugin_is_rejected_before_startup() -> None:
    # design §15 Phase 6 acceptance (incompatible half): requires a host tabvis version we are not.
    reg = PluginRegistry()
    reg.register(_candidate(id="too_new", requires=PluginRequirements(tabvis=">=0.4,<0.6")))
    report = reg.validate("too_new")
    assert not report.ok
    assert any("incompatible" in e for e in report.errors)
    assert reg.status("too_new") == REJECTED


def test_over_privileged_plugin_is_rejected_before_startup() -> None:
    # design §15 Phase 6 acceptance (over-privileged half).
    reg = PluginRegistry(policy=PermissionPolicy(grantable=("gateway:*",), granted=("gateway:*",)))
    reg.register(_candidate(id="greedy", permissions=("fs:/:read", "gateway:conversation.submit")))
    report = reg.validate("greedy")
    assert not report.ok
    assert any("over-privileged" in e for e in report.errors)
    assert reg.status("greedy") == REJECTED


def test_rejected_plugin_cannot_be_started() -> None:
    async def scenario() -> None:
        reg = PluginRegistry()
        reg.register(_candidate(id="too_new", requires=PluginRequirements(tabvis=">=99")))
        with pytest.raises(GatewayError):
            await reg.start("too_new")

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_builtin_plugins_validate_and_start_in_dependency_order() -> None:
    # design §8.7: browser engine, MCP, skill, and channel all run under one registry.
    async def scenario() -> None:
        reg = PluginRegistry()
        reg.register_all(builtin.builtin_candidates())
        await reg.start_all(services=None)
        ready = set(reg.ready())
        assert {"engine.chromium", "mcp.filesystem", "skill.deep-research", "channel.web"} <= ready
        assert (await reg.health("engine.chromium")).status == "ready"
        await reg.stop_all()
        assert reg.status("engine.chromium") == "stopped"

    asyncio.run(scenario())


def test_dependencies_start_before_dependents() -> None:
    async def scenario() -> None:
        reg = PluginRegistry()
        started: list[str] = []

        def make(pid, deps=()):
            manifest = PluginManifest(
                id=pid, version="1.0.0", kind=KIND_TOOL_PROVIDER,
                requires=PluginRequirements(tabvis=">=0.0", plugins=tuple(deps)),
            )

            class P(builtin.BuiltinPlugin):
                def __init__(self) -> None:
                    super().__init__(manifest)

                async def start(self, services) -> None:
                    started.append(pid)
                    await super().start(services)

            return PluginCandidate(manifest=manifest, factory=P)

        reg.register(make("dependent", deps=("base",)))
        reg.register(make("base"))
        await reg.start_all()
        assert started.index("base") < started.index("dependent")

    asyncio.run(scenario())


def test_optional_plugin_failure_degrades_only_itself() -> None:
    # design §15 Phase 6 acceptance: optional plugin failure does not stop core readiness.
    async def scenario() -> None:
        reg = PluginRegistry()

        good_manifest = PluginManifest(id="good", version="1.0.0", kind=KIND_TOOL_PROVIDER,
                                       requires=PluginRequirements(tabvis=">=0.0"))
        bad_manifest = PluginManifest(id="bad", version="1.0.0", kind=KIND_TOOL_PROVIDER,
                                      requires=PluginRequirements(tabvis=">=0.0"), optional=True)

        class Bad(builtin.BuiltinPlugin):
            def __init__(self) -> None:
                super().__init__(bad_manifest)

            async def start(self, services) -> None:
                raise RuntimeError("boom")

        reg.register(PluginCandidate(manifest=good_manifest, factory=lambda: builtin.BuiltinPlugin(good_manifest)))
        reg.register(PluginCandidate(manifest=bad_manifest, factory=Bad))
        await reg.start_all()

        assert "good" in reg.ready()      # the healthy plugin is unaffected
        assert "bad" in reg.degraded()    # the failure is contained to the bad plugin

    asyncio.run(scenario())


def test_required_plugin_failure_raises() -> None:
    async def scenario() -> None:
        reg = PluginRegistry()
        manifest = PluginManifest(id="core", version="1.0.0", kind=KIND_TOOL_PROVIDER,
                                  requires=PluginRequirements(tabvis=">=0.0"), optional=False)

        class Core(builtin.BuiltinPlugin):
            def __init__(self) -> None:
                super().__init__(manifest)

            async def start(self, services) -> None:
                raise RuntimeError("core down")

        reg.register(PluginCandidate(manifest=manifest, factory=Core))
        with pytest.raises(RuntimeError):
            await reg.start_all()

    asyncio.run(scenario())


def test_unmet_dependency_degrades_the_dependent() -> None:
    async def scenario() -> None:
        reg = PluginRegistry()
        # 'dependent' requires 'absent', which is never registered → dependent degrades, doesn't crash.
        reg.register(_candidate(id="dependent", requires=PluginRequirements(tabvis=">=0.0", plugins=("absent",))))
        reg.register(_candidate(id="solo"))
        await reg.start_all()
        assert "solo" in reg.ready()
        assert "dependent" in reg.degraded()

    asyncio.run(scenario())


def test_capabilities_and_effective_permissions_are_exposed() -> None:
    reg = PluginRegistry(policy=PermissionPolicy(grantable=("*",), granted=("tool:*",)))
    reg.register(_candidate(id="tp", capabilities=("tool.provide",), permissions=("tool:invoke", "net:egress")))
    reg.validate("tp")
    assert reg.capabilities("tp") == frozenset({"tool.provide"})
    assert reg.effective_permissions("tp") == ("tool:invoke",)  # net:egress not granted
