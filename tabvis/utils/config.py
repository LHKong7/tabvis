"""Config system

Skeleton scope: ``enable_configs`` is the gate that the TS calls before reading config
(it flips a module flag so config reads are permitted). The full project/global config
loading, file locking, and re-entrancy guard are planned for a later implementation phase.
"""

from __future__ import annotations

_configs_enabled = False


def enable_configs() -> None:
    """Permit config reads (TS: ``enableConfigs``). No-op-safe; idempotent."""
    global _configs_enabled
    _configs_enabled = True


def are_configs_enabled() -> bool:
    return _configs_enabled
