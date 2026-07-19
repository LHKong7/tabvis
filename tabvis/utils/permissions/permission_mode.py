"""Permission-mode schemas, config, and helpers.

Re-exports the permission-mode *types*/constants (extracted to ``src/types/permissions.ts`` to
break import cycles), defines the two Zod enum schemas (wrapped in ``lazySchema``), the per-mode
display config table, and the mode predicate/accessor helpers (title, short title, symbol, color,
external-mode coercion, string parsing).

Imports ``PAUSE_ICON`` from :mod:`tabvis.constants.figures` (the real implemented dep) for the plan-mode
symbol.

Zod -> pydantic v2: each ``z.enum(...)`` becomes a ``RootModel`` over the corresponding ``Literal``
union. ``PERMISSION_MODE_CONFIG`` keeps its UI string values verbatim; ``ModeColorKey`` / config
field names are internal (snake_case), not wire keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import RootModel

from tabvis.constants.figures import PAUSE_ICON

# Types extracted to tabvis/types/permissions.py to break import cycles.
# Re-exported here for backwards compatibility (parity with the TS re-export block).
from tabvis.types.permissions import (
    EXTERNAL_PERMISSION_MODES,
    PERMISSION_MODES,
    ExternalPermissionMode,
    PermissionMode,
)
from tabvis.utils.lazy_schema import lazy_schema

__all__ = [
    "EXTERNAL_PERMISSION_MODES",
    "PERMISSION_MODES",
    "ExternalPermissionMode",
    "ExternalPermissionModeSchema",
    "PermissionMode",
    "PermissionModeSchema",
    "external_permission_mode_schema",
    "get_mode_color",
    "is_default_mode",
    "is_external_permission_mode",
    "permission_mode_from_string",
    "permission_mode_schema",
    "permission_mode_short_title",
    "permission_mode_symbol",
    "permission_mode_title",
    "to_external_permission_mode",
]


class PermissionModeSchema(RootModel[PermissionMode]):
    """``z.enum(PERMISSION_MODES)`` — validates an internal permission mode."""

    root: PermissionMode


class ExternalPermissionModeSchema(RootModel[ExternalPermissionMode]):
    """``z.enum(EXTERNAL_PERMISSION_MODES)`` — validates an external permission mode."""

    root: ExternalPermissionMode


# Wrapped in lazy_schema to mirror the TS lazySchema(() => z.enum(...)).
permission_mode_schema = lazy_schema(lambda: PermissionModeSchema)
external_permission_mode_schema = lazy_schema(lambda: ExternalPermissionModeSchema)


ModeColorKey = Literal[
    "text", "planMode", "permission", "autoAccept", "error", "warning"
]


@dataclass(frozen=True)
class PermissionModeConfig:
    title: str
    short_title: str
    symbol: str
    color: ModeColorKey
    external: ExternalPermissionMode


# Partial<Record<PermissionMode, PermissionModeConfig>> — modes without an entry fall back to
# the 'default' config via _get_mode_config.
PERMISSION_MODE_CONFIG: dict[PermissionMode, PermissionModeConfig] = {
    "default": PermissionModeConfig(
        title="Default",
        short_title="Default",
        symbol="",
        color="text",
        external="default",
    ),
    "plan": PermissionModeConfig(
        title="Plan Mode",
        short_title="Plan",
        symbol=PAUSE_ICON,
        color="planMode",
        external="plan",
    ),
    "acceptEdits": PermissionModeConfig(
        title="Accept edits",
        short_title="Accept",
        symbol="⏵⏵",
        color="autoAccept",
        external="acceptEdits",
    ),
    "bypassPermissions": PermissionModeConfig(
        title="Bypass Permissions",
        short_title="Bypass",
        symbol="⏵⏵",
        color="error",
        external="bypassPermissions",
    ),
    "dontAsk": PermissionModeConfig(
        title="Don't Ask",
        short_title="DontAsk",
        symbol="⏵⏵",
        color="error",
        external="dontAsk",
    ),
}


def is_external_permission_mode(mode: PermissionMode) -> bool:
    """Type guard: every mode except the internal ``'bubble'`` is external."""
    return mode != "bubble"


def _get_mode_config(mode: PermissionMode) -> PermissionModeConfig:
    """Look up ``mode``'s config, falling back to the ``'default'`` entry (TS ``?? .default!``)."""
    return PERMISSION_MODE_CONFIG.get(mode) or PERMISSION_MODE_CONFIG["default"]


def to_external_permission_mode(mode: PermissionMode) -> ExternalPermissionMode:
    return _get_mode_config(mode).external


def permission_mode_from_string(value: str) -> PermissionMode:
    """Coerce an arbitrary string to a :data:`PermissionMode`, defaulting to ``'default'``."""
    return value if value in PERMISSION_MODES else "default"  # type: ignore[return-value]


def permission_mode_title(mode: PermissionMode) -> str:
    return _get_mode_config(mode).title


def is_default_mode(mode: PermissionMode | None) -> bool:
    return mode == "default" or mode is None


def permission_mode_short_title(mode: PermissionMode) -> str:
    return _get_mode_config(mode).short_title


def permission_mode_symbol(mode: PermissionMode) -> str:
    return _get_mode_config(mode).symbol


def get_mode_color(mode: PermissionMode) -> ModeColorKey:
    return _get_mode_config(mode).color
