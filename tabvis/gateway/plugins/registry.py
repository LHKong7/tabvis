"""PluginRegistry — discovery, validation, and lifecycle for every plugin kind (design §8.3, §8.4, §8.5).

The registry drives each plugin through the §8.3 lifecycle and upholds the design's guarantees:

* validation runs before startup; a rejected candidate never loads (design §8.5);
* validated plugins start in topological dependency order and stop in reverse (design §8.5);
* an **optional** plugin that fails to start (or whose dependency is unsatisfied) goes ``degraded`` and
  does not stop the others or core readiness (design §8.5, §15 Phase 6); a **required** plugin's failure
  raises.

A plugin is handed a capability-scoped ``services`` object on start — never the registry or a store
(design §8.6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tabvis.gateway.plugins import validation as validation_mod
from tabvis.gateway.plugins.contract import (
    DEGRADED,
    DISCOVERED,
    LOADED,
    READY,
    REJECTED,
    STARTED,
    STOPPED,
    STOPPING,
    VALIDATED,
    Plugin,
    PluginCandidate,
    PluginHealth,
    ValidationReport,
)
from tabvis.gateway.plugins.dependency import topological_order
from tabvis.gateway.plugins.permissions import LOCAL_POLICY, PermissionPolicy
from tabvis.utils.debug import log_for_debugging


@dataclass
class _Record:
    candidate: PluginCandidate
    state: str = DISCOVERED
    report: ValidationReport | None = None
    instance: Plugin | None = None
    detail: str | None = None


@dataclass
class PluginRegistry:
    policy: PermissionPolicy = LOCAL_POLICY
    host_capabilities: frozenset[str] = frozenset()
    _records: dict[str, _Record] = field(default_factory=dict)

    # --- discovery ------------------------------------------------------------------------------

    def register(self, candidate: PluginCandidate) -> None:
        self._records[candidate.manifest.id] = _Record(candidate=candidate, state=DISCOVERED)

    def register_all(self, candidates: list[PluginCandidate]) -> None:
        for c in candidates:
            self.register(c)

    def ids(self) -> list[str]:
        return list(self._records)

    # --- validation -----------------------------------------------------------------------------

    def validate(self, plugin_id: str) -> ValidationReport:
        rec = self._require(plugin_id)
        report = validation_mod.validate(
            rec.candidate, policy=self.policy, host_capabilities=self.host_capabilities
        )
        rec.report = report
        rec.state = VALIDATED if report.ok else REJECTED
        if not report.ok:
            rec.detail = "; ".join(report.errors)
        return report

    def validate_all(self) -> dict[str, ValidationReport]:
        return {pid: self.validate(pid) for pid in list(self._records)}

    # --- lifecycle ------------------------------------------------------------------------------

    async def start_all(self, services: Any = None) -> None:
        """Validate (if needed) then start every accepted plugin in dependency order (design §8.5)."""
        for pid, rec in self._records.items():
            if rec.report is None:
                self.validate(pid)

        startable = {pid for pid, rec in self._records.items() if rec.state == VALIDATED}

        # A plugin whose dependency was rejected/absent cannot start; degrade it and drop it from the
        # graph so one bad dependency doesn't sink the whole start (design §8.5 optional-failure).
        graph: dict[str, tuple[str, ...]] = {}
        for pid in list(startable):
            deps = self._records[pid].candidate.manifest.requires.plugins
            unmet = [d for d in deps if d not in startable]
            if unmet:
                self._degrade(pid, f"unmet plugin dependencies {unmet}")
                startable.discard(pid)
        for pid in startable:
            graph[pid] = tuple(d for d in self._records[pid].candidate.manifest.requires.plugins if d in startable)

        for pid in topological_order(graph):
            await self._start_one(pid, services)

    async def start(self, plugin_id: str, services: Any = None) -> None:
        rec = self._require(plugin_id)
        if rec.report is None:
            self.validate(plugin_id)
        if rec.state == REJECTED:
            from tabvis.gateway.protocol.errors import GatewayError

            raise GatewayError("VALIDATION_FAILED", message=f"Plugin {plugin_id!r} rejected: {rec.detail}")
        await self._start_one(plugin_id, services)

    async def _start_one(self, plugin_id: str, services: Any) -> None:
        rec = self._records[plugin_id]
        try:
            if rec.candidate.factory is None:
                raise RuntimeError("no factory to instantiate plugin")
            rec.instance = rec.candidate.factory()
            rec.state = LOADED
            await rec.instance.start(services)
            rec.state = READY
            rec.detail = None
        except Exception as e:  # noqa: BLE001
            if rec.candidate.manifest.optional:
                self._degrade(plugin_id, f"start failed: {e}")
            else:
                rec.state = REJECTED
                rec.detail = f"required plugin failed: {e}"
                raise

    async def stop(self, plugin_id: str) -> None:
        rec = self._require(plugin_id)
        if rec.instance is not None:
            rec.state = STOPPING
            try:
                await rec.instance.stop()
            except Exception as e:  # noqa: BLE001
                log_for_debugging(f"[PLUGIN] stop failed for {plugin_id}: {e}")
        rec.state = STOPPED

    async def stop_all(self) -> None:
        """Stop in reverse dependency order (design §8.5)."""
        started = {pid: rec for pid, rec in self._records.items() if rec.state in (READY, DEGRADED)}
        graph = {pid: tuple(d for d in rec.candidate.manifest.requires.plugins if d in started)
                 for pid, rec in started.items()}
        for pid in reversed(topological_order(graph)):
            await self.stop(pid)

    # --- introspection --------------------------------------------------------------------------

    def status(self, plugin_id: str) -> str:
        return self._require(plugin_id).state

    def capabilities(self, plugin_id: str) -> frozenset[str]:
        return frozenset(self._require(plugin_id).candidate.manifest.capabilities)

    def effective_permissions(self, plugin_id: str) -> tuple[str, ...]:
        rec = self._require(plugin_id)
        return rec.report.effective_permissions if rec.report else ()

    async def health(self, plugin_id: str) -> PluginHealth:
        rec = self._require(plugin_id)
        if rec.instance is not None and rec.state in (READY, DEGRADED):
            try:
                return await rec.instance.health()
            except Exception as e:  # noqa: BLE001
                return PluginHealth(status="degraded", detail=str(e))
        return PluginHealth(status=rec.state, detail=rec.detail)

    def ready(self) -> list[str]:
        return [pid for pid, rec in self._records.items() if rec.state == READY]

    def degraded(self) -> list[str]:
        return [pid for pid, rec in self._records.items() if rec.state == DEGRADED]

    def rejected(self) -> list[str]:
        return [pid for pid, rec in self._records.items() if rec.state == REJECTED]

    def _degrade(self, plugin_id: str, detail: str) -> None:
        rec = self._records[plugin_id]
        rec.state = DEGRADED
        rec.detail = detail
        log_for_debugging(f"[PLUGIN] {plugin_id} degraded: {detail}")

    def _require(self, plugin_id: str) -> _Record:
        rec = self._records.get(plugin_id)
        if rec is None:
            from tabvis.gateway.protocol.errors import GatewayError

            raise GatewayError("NOT_FOUND", message=f"No plugin {plugin_id!r}")
        return rec
