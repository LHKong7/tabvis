"""Environment helpers"""

from __future__ import annotations

import os
import unicodedata


def get_tabvis_config_home_dir() -> str:
    """Tabvis config home dir: ``TABVIS_CONFIG_DIR`` or ``~/.tabvis`` (NFC-normalized)."""
    base = os.environ.get("TABVIS_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".tabvis"
    )
    return unicodedata.normalize("NFC", base)


def is_env_truthy(env_var: str | bool | None) -> bool:
    if not env_var:
        return False
    if isinstance(env_var, bool):
        return env_var
    return env_var.lower().strip() in ("1", "true", "yes", "on")


def is_env_defined_falsy(env_var: str | bool | None) -> bool:
    if env_var is None:
        return False
    if isinstance(env_var, bool):
        return not env_var
    if not env_var:
        return False
    return env_var.lower().strip() in ("0", "false", "no", "off")
