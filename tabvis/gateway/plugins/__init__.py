"""Plugin Runtime — shared discovery, lifecycle, capability, and permission rules (design §8).

Tabvis has several extension mechanisms with *different* execution models — MCP servers (tools),
Skills (prompt/workflow), browser engines (drivers), hooks, and channels. The Plugin Runtime gives them
one discovery/validation/lifecycle/permission spine **without forcing them to share a mechanism**
(design §8.1): each is wrapped as a built-in provider (§8.7) that plugs into the same registry.

Two guarantees the design pins here (§15 Phase 6):

* an incompatible (version/capability) or over-privileged (permissions beyond the policy ceiling)
  plugin is rejected **before** its entrypoint runs;
* an optional plugin's failure degrades only its feature — core readiness is unaffected.
"""

from __future__ import annotations
