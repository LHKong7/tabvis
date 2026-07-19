"""Memory-type values

The TS module is::

    export const MEMORY_TYPE_VALUES = [
      'User', 'Project', 'Local', 'Managed', 'AutoMem',
      ...(false ? (['TeamMem'] as const) : []),
    ] as const
    export type MemoryType = (typeof MEMORY_TYPE_VALUES)[number]

The trailing ``...(false ? [...] : [])`` spread is a dead-gated ``'TeamMem'`` entry — the
guard is a literal ``false``, so it never contributes a value (faithful to the TS, where the
ant-only ``TeamMem`` scope is compiled out). We reproduce the gate explicitly so the dead
branch is visible.

Python has no runtime ``type`` alias for a string-literal union, so we expose the contract
two ways:

- :data:`MEMORY_TYPE_VALUES` — the runtime ordered tuple of valid memory-type wire values.
- :data:`MemoryType` — a :data:`typing.Literal` for static typing / annotations.

The literal values (``'User'``/``'Project'``/``'Local'``/``'Managed'``/``'AutoMem'``) are
wire-shaped scope identifiers and are kept verbatim.
"""

from __future__ import annotations

from typing import Literal

# Dead-gated ``TeamMem`` scope — the TS guard is a literal `false`, so it never contributes.
_TEAM_MEM_ENABLED = False

MEMORY_TYPE_VALUES: tuple[str, ...] = (
    "User",
    "Project",
    "Local",
    "Managed",
    "AutoMem",
    *(("TeamMem",) if _TEAM_MEM_ENABLED else ()),
)

# String-literal union mirroring the TS ``MemoryType`` type alias (for annotations only).
MemoryType = Literal["User", "Project", "Local", "Managed", "AutoMem"]

__all__ = ["MEMORY_TYPE_VALUES", "MemoryType"]
