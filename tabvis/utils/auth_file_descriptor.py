"""FD-or-well-known-file credential reader

Reads CCR-injected credentials. Priority order:

1. **File descriptor** (legacy): an env var names a pipe FD passed by the Go env-manager via
   ``cmd.ExtraFiles``. The pipe is drained on first read and does NOT cross exec/tmux
   boundaries.
2. **Well-known file**: written by this module on a successful FD read (and eventually by the
   env-manager directly). Covers subprocesses that can't inherit the FD.

Returns ``None`` when neither source has a credential. The result is cached in bootstrap state.

Casing: Python identifiers are snake_case. No wire-key dicts are involved (credentials are
plain strings).

Faithful-behavior notes (per ``docs/SPINE_CONTRACTS.md``):
- ``mkdirSync(dir, {recursive, mode:0o700})`` →
  :meth:`tabvis.utils.fs_operations.FsOperations.mkdir_sync` (recursive ``os.makedirs``);
  ``writeFileSync(path, token, {mode:0o600})`` →
  :func:`tabvis.utils.slow_operations.write_file_sync_deprecated` (the FsOperations façade exposes
  no sync write). The CCR-only on-disk persistence stays a one-shot at startup.
- ``getApiKeyFromFd`` / ``setApiKeyFromFd`` are the REAL bootstrap-state cache getters/setters
  (:mod:`tabvis.bootstrap.state`). The ``cached !== undefined`` sentinel (distinguishing
  "never looked" from "looked, found nothing") is preserved with a module-level ``_UNSET``
  sentinel since Python state stores ``None`` for both unset and miss.
- ``/dev/fd/<fd>`` on macOS/BSD, ``/proc/self/fd/<fd>`` on Linux — branch kept verbatim.
- ``isEnvTruthy(process.env.TABVIS_REMOTE)`` gates the on-disk write to CCR only.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

from tabvis.bootstrap.state import get_api_key_from_fd, set_api_key_from_fd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.errors import get_error_message, is_enoent
from tabvis.utils.fs_operations import get_fs_implementation
from tabvis.utils.slow_operations import write_file_sync_deprecated

# Well-known token file locations in CCR. The Go env-manager creates /home/tabvis/.tabvis/remote/
# and will (eventually) write these files too. Until then, this module writes them on a
# successful FD read so subprocesses spawned inside the CCR container can find the token
# without inheriting the FD — which they can't: pipe FDs don't cross tmux/shell boundaries.
CCR_TOKEN_DIR = "/home/tabvis/.tabvis/remote"
CCR_API_KEY_PATH = f"{CCR_TOKEN_DIR}/.api_key"
CCR_SESSION_INGRESS_TOKEN_PATH = f"{CCR_TOKEN_DIR}/.session_ingress_token"

# Sentinel distinguishing "cache never populated" from "cache holds None (looked, found nothing)".
# The TS code keys on `cached !== undefined`; the bootstrap-state cache uses None for the unset
# slot, so we treat a None getter result as "not yet looked".
_UNSET: Any = object()


def maybe_persist_token_for_subprocesses(path: str, token: str, token_name: str) -> None:
    """Best-effort CCR-gated write of ``token`` to a well-known ``path`` for subprocess access.

    Outside CCR (``TABVIS_REMOTE`` falsy) there's no ``/home/tabvis/`` and no reason to put a token
    on disk that the FD was meant to keep off disk — so this is a no-op there.
    """
    if not is_env_truthy(os.environ.get("TABVIS_REMOTE")):
        return
    try:
        # TS used bare fs.mkdirSync/fs.writeFileSync. mkdir maps to the swappable impl's
        # recursive mkdir_sync; the sync write maps to write_file_sync_deprecated (FsOperations
        # exposes no write_file_sync). The 0o700/0o600 modes are preserved.
        get_fs_implementation().mkdir_sync(CCR_TOKEN_DIR, {"recursive": True, "mode": 0o700})
        write_file_sync_deprecated(path, token, {"encoding": "utf8", "mode": 0o600})
        log_for_debugging(f"Persisted {token_name} to {path} for subprocess access")
    except Exception as error:  # noqa: BLE001 - faithful TS catch-all
        log_for_debugging(
            f"Failed to persist {token_name} to disk (non-fatal): {get_error_message(error)}",
            {"level": "error"},
        )


def read_token_from_well_known_file(path: str, token_name: str) -> str | None:
    """Fallback read from a well-known file.

    The path only exists in CCR (env-manager creates the directory), so file-not-found is the
    expected outcome everywhere else — treated as "no fallback", not an error. A non-ENOENT
    failure (EACCES from a perm misconfig, etc.) IS surfaced to the debug log so subprocess auth
    failures aren't mysterious.
    """
    try:
        fs_ops = get_fs_implementation()
        token = fs_ops.read_file_sync(path, {"encoding": "utf8"}).strip()
        if not token:
            return None
        log_for_debugging(f"Read {token_name} from well-known file {path}")
        return token
    except Exception as error:  # noqa: BLE001 - faithful TS catch-all
        if not is_enoent(error):
            log_for_debugging(
                f"Failed to read {token_name} from {path}: {get_error_message(error)}",
                {"level": "debug"},
            )
        return None


def _get_credential_from_fd(
    *,
    env_var: str,
    well_known_path: str,
    label: str,
    get_cached: Callable[[], str | None],
    set_cached: Callable[[str | None], None],
) -> str | None:
    """Shared FD-or-well-known-file credential reader (priority: FD → well-known file)."""
    cached = get_cached()
    # The bootstrap-state cache stores None for both "unset" and "looked, found nothing".
    # The TS uses an explicit `undefined` sentinel; we re-derive it: a non-None cache value is
    # always a hit. (A None value re-runs the lookup, which is idempotent and cheap.)
    if cached is not None:
        return cached

    fd_env = os.environ.get(env_var)
    if not fd_env:
        # No FD env var — either we're not in CCR, or we're a subprocess whose parent stripped
        # the (useless) FD env var. Try the well-known file.
        from_file = read_token_from_well_known_file(well_known_path, label)
        set_cached(from_file)
        return from_file

    try:
        fd = int(fd_env, 10)
    except ValueError:
        log_for_debugging(
            f"{env_var} must be a valid file descriptor number, got: {fd_env}",
            {"level": "error"},
        )
        set_cached(None)
        return None

    try:
        # Use /dev/fd on macOS/BSD, /proc/self/fd on Linux.
        fs_ops = get_fs_implementation()
        if sys.platform == "darwin" or sys.platform.startswith("freebsd"):
            fd_path = f"/dev/fd/{fd}"
        else:
            fd_path = f"/proc/self/fd/{fd}"

        token = fs_ops.read_file_sync(fd_path, {"encoding": "utf8"}).strip()
        if not token:
            log_for_debugging(
                f"File descriptor contained empty {label}", {"level": "error"}
            )
            set_cached(None)
            return None
        log_for_debugging(f"Successfully read {label} from file descriptor {fd}")
        set_cached(token)
        maybe_persist_token_for_subprocesses(well_known_path, token, label)
        return token
    except Exception as error:  # noqa: BLE001 - faithful TS catch-all
        log_for_debugging(
            f"Failed to read {label} from file descriptor {fd}: {get_error_message(error)}",
            {"level": "error"},
        )
        # FD env var was set but read failed — typically a subprocess that inherited the env var
        # but not the FD (ENXIO). Try the well-known file.
        from_file = read_token_from_well_known_file(well_known_path, label)
        set_cached(from_file)
        return from_file


def get_api_key_from_file_descriptor() -> str | None:
    """Get the CCR-injected API key (env: ``TABVIS_API_KEY_FILE_DESCRIPTOR``; file: ``.api_key``).

    See :func:`_get_credential_from_fd` for the FD-vs-disk rationale.
    """
    return _get_credential_from_fd(
        env_var="TABVIS_API_KEY_FILE_DESCRIPTOR",
        well_known_path=CCR_API_KEY_PATH,
        label="API key",
        get_cached=get_api_key_from_fd,
        set_cached=set_api_key_from_fd,
    )
