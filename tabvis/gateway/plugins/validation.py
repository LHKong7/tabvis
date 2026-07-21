"""Plugin validation — reject the incompatible and the over-privileged before startup (design §8.5, §8.6).

Every check here runs *before* a plugin's entrypoint executes (design §8.5), so a bad plugin never gets
to run code. The checks:

* structural — a known schema version and kind;
* host compatibility — ``requires.tabvis`` satisfied by the running tabvis version;
* capability compatibility — every required host capability is offered by the host;
* permissions — nothing requested outside the deployment's grantable ceiling (over-privileged → reject).

A failing report means the plugin is rejected; a passing report carries the effective permission set it
will run with.
"""

from __future__ import annotations

from tabvis import __version__ as TABVIS_VERSION
from tabvis.gateway.plugins import version as version_mod
from tabvis.gateway.plugins.contract import PluginCandidate, ValidationReport
from tabvis.gateway.plugins.manifest import KNOWN_KINDS, SCHEMA_VERSION
from tabvis.gateway.plugins.permissions import LOCAL_POLICY, PermissionPolicy


def validate(
    candidate: PluginCandidate,
    *,
    policy: PermissionPolicy = LOCAL_POLICY,
    host_capabilities: frozenset[str] = frozenset(),
    host_version: str = TABVIS_VERSION,
) -> ValidationReport:
    manifest = candidate.manifest
    errors: list[str] = []

    if manifest.schema_version > SCHEMA_VERSION:
        errors.append(f"unsupported schema_version {manifest.schema_version} (max {SCHEMA_VERSION})")
    if manifest.kind not in KNOWN_KINDS:
        errors.append(f"unknown kind {manifest.kind!r}")

    if not version_mod.satisfies(host_version, manifest.requires.tabvis):
        errors.append(
            f"incompatible: requires tabvis {manifest.requires.tabvis!r}, host is {host_version}"
        )

    missing_caps = [c for c in manifest.requires.capabilities if c not in host_capabilities]
    if missing_caps:
        errors.append(f"incompatible: host lacks required capabilities {missing_caps}")

    over = policy.over_privileged(manifest.permissions)
    if over:
        errors.append(f"over-privileged: requests un-grantable permissions {over}")

    effective = policy.effective(manifest.permissions)
    return ValidationReport(
        plugin_id=manifest.id, ok=not errors, errors=errors, effective_permissions=effective
    )
