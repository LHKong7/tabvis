"""Environment-config helpers for platform channel plugins.

Every platform reads its credentials from ``TABVIS_<PLATFORM>_*`` variables. These helpers keep the
per-platform ``from_env`` builders short and consistent, and accept an explicit ``env`` mapping so a
test can construct a config without touching ``os.environ``.
"""

from __future__ import annotations

import os
from typing import Mapping


def _source(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return env if env is not None else os.environ


def env_str(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """A trimmed string value, or ``default`` when unset/blank."""
    value = _source(env).get(name)
    return value if value else default


def env_required(name: str, env: Mapping[str, str] | None = None) -> str:
    """A required value; raises a clear error naming the missing variable."""
    value = _source(env).get(name)
    if not value:
        raise RuntimeError(f"{name} is required to configure this channel")
    return value


def env_bool(name: str, default: bool = False, env: Mapping[str, str] | None = None) -> bool:
    value = _source(env).get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
