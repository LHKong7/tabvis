"""Plugin permission model (design §8.6).

Manifest permissions are the *maximum requested*, never grants (design §8.6). Two policy sets shape
the outcome:

* ``grantable`` — the deployment's ceiling: permissions that *may* be granted at all. A request outside
  this ceiling is **over-privileged** and the plugin is rejected before startup (design §15 Phase 6).
* ``granted`` — what the administrator actually grants (a subset of grantable).

The effective permission set a plugin runs with is ``requested ∩ granted ∩ runtime policy`` (design
§8.6); here ``granted`` already folds in the runtime policy. Patterns match with shell globbing, so
``secret:feishu.*:read`` is covered by a grant of ``secret:feishu.*:read`` or ``secret:*``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass


def _covered(permission: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(permission, pattern) for pattern in patterns)


@dataclass(frozen=True)
class PermissionPolicy:
    grantable: tuple[str, ...] = ("*",)   # ceiling — default: anything may be granted (local/dev)
    granted: tuple[str, ...] = ("*",)     # actually granted — default: all

    def over_privileged(self, requested: tuple[str, ...]) -> list[str]:
        """Requested permissions outside the grantable ceiling — cause for rejection (design §8.6)."""
        return [p for p in requested if not _covered(p, self.grantable)]

    def effective(self, requested: tuple[str, ...]) -> tuple[str, ...]:
        """The permissions the plugin actually runs with: requested ∩ granted (design §8.6)."""
        return tuple(p for p in requested if _covered(p, self.granted))


# The permissive local/dev default: everything is grantable and granted (matches today's posture).
LOCAL_POLICY = PermissionPolicy(grantable=("*",), granted=("*",))
