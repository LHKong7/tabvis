"""Session-ingress auth token resolution

Resolves the session-ingress bearer token used to reach the session host. Priority order:

1. Environment variable (``TABVIS_SESSION_ACCESS_TOKEN``) — set at spawn time, updated in-process
   via :func:`update_session_ingress_auth_token` or an ``update_environment_variables`` stdin
   message from the parent process.
2. File descriptor (legacy path) — ``TABVIS_WEBSOCKET_AUTH_FILE_DESCRIPTOR``, read once and cached
   in bootstrap state.
3. Well-known file — ``TABVIS_SESSION_INGRESS_TOKEN_FILE`` env path, or the CCR default
   (:data:`tabvis.utils.auth_file_descriptor.CCR_SESSION_INGRESS_TOKEN_PATH`). Covers subprocesses
   that can't inherit the FD.

Casing: Python identifiers are snake_case. :func:`get_session_ingress_auth_headers` returns a
``dict[str, str]`` whose ``Authorization`` key is an HTTP wire header — kept verbatim.

Faithful-behavior notes:
- ``getSessionIngressToken`` / ``setSessionIngressToken`` are the REAL
  :mod:`tabvis.bootstrap.state` cache getters/setters.
- ``readTokenFromWellKnownFile`` / ``maybePersistTokenForSubprocesses`` /
  ``CCR_SESSION_INGRESS_TOKEN_PATH`` are the REAL implemented
  :mod:`tabvis.utils.auth_file_descriptor` surfaces.
- The TS keys on ``getSessionIngressToken() !== undefined`` to distinguish "never looked" from
  "looked, cached null". The Python bootstrap state stores ``None`` for both, so — exactly like
  :func:`tabvis.utils.auth_file_descriptor._get_credential_from_fd` — a ``None`` cache value re-runs
  the lookup (idempotent and cheap), and a non-``None`` value is always treated as a hit.
- ``/dev/fd/<fd>`` on macOS/BSD, ``/proc/self/fd/<fd>`` on Linux — branch kept verbatim.
"""

from __future__ import annotations

import os
import sys

from tabvis.bootstrap.state import (
    get_session_ingress_token,
    set_session_ingress_token,
)
from tabvis.utils.auth_file_descriptor import (
    CCR_SESSION_INGRESS_TOKEN_PATH,
    maybe_persist_token_for_subprocesses,
    read_token_from_well_known_file,
)
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.errors import get_error_message
from tabvis.utils.fs_operations import get_fs_implementation


def _well_known_token_path() -> str:
    """``TABVIS_SESSION_INGRESS_TOKEN_FILE`` env path, or the CCR default."""
    return os.environ.get("TABVIS_SESSION_INGRESS_TOKEN_FILE") or CCR_SESSION_INGRESS_TOKEN_PATH


def _get_token_from_file_descriptor() -> str | None:
    """Read token via file descriptor, falling back to the well-known file.

    Uses bootstrap state to cache the result since file descriptors can only be read once.
    """
    # Check if we've already attempted to read the token. (None re-runs the lookup — see module
    # docstring on the missing `undefined` sentinel.)
    cached_token = get_session_ingress_token()
    if cached_token is not None:
        return cached_token

    fd_env = os.environ.get("TABVIS_WEBSOCKET_AUTH_FILE_DESCRIPTOR")
    if not fd_env:
        # No FD env var — either we're not in CCR, or we're a subprocess whose parent stripped the
        # (useless) FD env var. Try the well-known file.
        from_file = read_token_from_well_known_file(
            _well_known_token_path(), "session ingress token"
        )
        set_session_ingress_token(from_file)
        return from_file

    try:
        fd = int(fd_env, 10)
    except ValueError:
        log_for_debugging(
            f"TABVIS_WEBSOCKET_AUTH_FILE_DESCRIPTOR must be a valid file descriptor number, "
            f"got: {fd_env}",
            {"level": "error"},
        )
        set_session_ingress_token(None)
        return None

    try:
        # Read from the file descriptor. Use /dev/fd on macOS/BSD, /proc/self/fd on Linux.
        fs_ops = get_fs_implementation()
        if sys.platform == "darwin" or sys.platform.startswith("freebsd"):
            fd_path = f"/dev/fd/{fd}"
        else:
            fd_path = f"/proc/self/fd/{fd}"

        token = fs_ops.read_file_sync(fd_path, {"encoding": "utf8"}).strip()
        if not token:
            log_for_debugging(
                "File descriptor contained empty token", {"level": "error"}
            )
            set_session_ingress_token(None)
            return None
        log_for_debugging(f"Successfully read token from file descriptor {fd}")
        set_session_ingress_token(token)
        maybe_persist_token_for_subprocesses(
            CCR_SESSION_INGRESS_TOKEN_PATH, token, "session ingress token"
        )
        return token
    except Exception as error:  # noqa: BLE001 - faithful TS catch-all
        log_for_debugging(
            f"Failed to read token from file descriptor {fd}: {get_error_message(error)}",
            {"level": "error"},
        )
        # FD env var was set but read failed — typically a subprocess that inherited the env var
        # but not the FD (ENXIO). Try the well-known file.
        from_file = read_token_from_well_known_file(
            _well_known_token_path(), "session ingress token"
        )
        set_session_ingress_token(from_file)
        return from_file


def get_session_ingress_auth_token() -> str | None:
    """Get the session-ingress authentication token (see module docstring for priority order)."""
    # 1. Check environment variable.
    env_token = os.environ.get("TABVIS_SESSION_ACCESS_TOKEN")
    if env_token:
        return env_token

    # 2. Check file descriptor (legacy path), with file fallback.
    return _get_token_from_file_descriptor()


def get_session_ingress_auth_headers() -> dict[str, str]:
    """Build auth headers for the current session token. Session ingress uses bearer auth."""
    token = get_session_ingress_auth_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def update_session_ingress_auth_token(token: str) -> None:
    """Update the session-ingress auth token in-process by setting the env var.

    Used by external session hosts to inject a fresh token after reconnection without restarting
    the process.
    """
    os.environ["TABVIS_SESSION_ACCESS_TOKEN"] = token
