"""Build-time macros.

Exposes version/build metadata (VERSION, BUILD_TIME, PACKAGE_URL, VERSION_CHANGELOG) as a
module-level ``MACRO`` namespace, plus :func:`ensure_bootstrap_macro` to guarantee it's
populated before use.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata


@dataclass(frozen=True)
class MacroConfig:
    VERSION: str
    BUILD_TIME: str
    PACKAGE_URL: str
    VERSION_CHANGELOG: str


def _default_macro() -> MacroConfig:
    try:
        version = metadata.version("tabvis")
    except metadata.PackageNotFoundError:  # running from a source checkout pre-install
        from tabvis import __version__ as version
    return MacroConfig(
        VERSION=version,
        BUILD_TIME="",
        PACKAGE_URL="tabvis",
        VERSION_CHANGELOG="",
    )


MACRO: MacroConfig = _default_macro()


def ensure_bootstrap_macro() -> None:
    """Ensure ``MACRO`` is populated.

    ``MACRO`` is already initialized as a module-level constant at import time, so this is
    effectively a no-op — it exists as a stable call site for code that wants to guarantee
    ``MACRO`` is ready before reading it.
    """

    global MACRO
    if MACRO is None:  # pragma: no cover - defensive
        MACRO = _default_macro()
