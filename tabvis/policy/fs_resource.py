"""Filesystem path → policy resource classification (PP-7).

``docs/permission-policy-engine_v1.md`` §5.2 / §7 (Filesystem): turn an absolute-or-relative file path
into a normalized policy resource, resolving symlinks so a link cannot smuggle a write out of the
workspace or into a protected area. A path is classified into one of:

* ``workspace:<rel>`` — under the session's project root (``get_original_cwd``).
* ``secret:<real>``   — a sensitive secret file (``.env``/``.env.*``, ``*.key``/``*.pem``/``*.p12``/
  ``*.pfx``, ``storage-state*``, ``credentials.json``, a browser working profile). Hard write
  protection always; read protection under strict mode.
* ``config:<real>``   — under the config home but not itself a secret. Hard write protection.
* ``fs:<real>``       — anything else (out-of-workspace). Allowed by the lenient FS baseline; under
  strict mode it needs a directory grant (``locked`` denies, ``standard`` asks).

The path is ``realpath``-resolved *before* classification, so a symlink inside the workspace that
points at ``/etc`` or the config home classifies by its **real** target — closing the symlink-escape
hole. Residual TOCTOU (a path that turns into a symlink between this check and the write) is a
side-effect-point concern noted for a later refinement; classification here always reflects the link
state at check time.
"""

from __future__ import annotations

import os


def _real(path: str) -> str:
    return os.path.realpath(path)


def _is_under(child: str, parent: str) -> bool:
    """True if ``child`` is ``parent`` or nested under it (both already realpath-resolved)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False  # different drives / mixed absolute+relative


_SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_SENSITIVE_NAMES = frozenset({".env", "storage-state.enc", "storage-state.json", "credentials.json"})
_SENSITIVE_SEGMENTS = frozenset({"working-profile", "user-data-dir"})


def _is_sensitive(real: str) -> bool:
    name = os.path.basename(real)
    if name in _SENSITIVE_NAMES or name.startswith(".env."):
        return True
    if name.endswith(_SENSITIVE_SUFFIXES):
        return True
    parts = set(real.split(os.sep))
    return bool(parts & _SENSITIVE_SEGMENTS)


def classify_path(
    path: str,
    *,
    cwd: str | None = None,
    config_home: str | None = None,
) -> str:
    """Classify ``path`` into a policy resource string (``workspace:`` / ``config:`` / ``fs:``).

    ``cwd`` defaults to the session project root and ``config_home`` to the tabvis config home. The path
    is expanded (``~``, relative → ``cwd``) and realpath-resolved before classification.
    """
    if cwd is None:
        from tabvis.bootstrap.state import get_original_cwd

        cwd = get_original_cwd()
    if config_home is None:
        from tabvis.utils.env_utils import get_tabvis_config_home_dir

        config_home = get_tabvis_config_home_dir()

    from tabvis.utils.path import expand_path

    abs_path = expand_path(path, base_dir=cwd)
    real = _real(abs_path)
    real_cwd = _real(cwd)
    real_cfg = _real(config_home)

    if _is_sensitive(real):
        return f"secret:{real}"

    # The download workspace is a WRITABLE working area for the agent (browser downloads + files it
    # creates to process them). It sits under the config home by default, so classify it as
    # ``workspace:`` BEFORE the ``config:`` hard-deny — otherwise the agent could read but never write
    # there. (A secret dropped inside it was already caught above and stays ``secret:``.)
    try:
        from tabvis.browser.downloads import get_workspace_dir

        real_ws = _real(get_workspace_dir())
        if _is_under(real, real_ws):
            rel = os.path.relpath(real, real_ws)
            return f"workspace:{rel}"
    except Exception:  # noqa: BLE001 — never let workspace resolution break classification
        pass

    if _is_under(real, real_cfg):
        return f"config:{real}"
    if _is_under(real, real_cwd):
        rel = os.path.relpath(real, real_cwd)
        return f"workspace:{rel}"
    return f"fs:{real}"
