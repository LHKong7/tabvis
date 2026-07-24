"""Browser secret store (IDP-6) — ``secret_ref`` indirection over a real OS credential store.

``design.md`` §"数据存储": passwords / tokens / proxy keys and the data-encryption key live in the OS
Keychain; the DB and records keep only a ``secret_ref``. This is that store: :func:`put` returns an
opaque ref, :func:`get` resolves it, so an identity/record never holds the plaintext.

Backend selection (issue #6). A **secure OS-backed store is the default**, not opt-in:

* ``TABVIS_SECRET_BACKEND=file|keychain|keyring`` forces a specific backend (used by the test suite
  to pin ``file`` so it never touches the developer's real keystore).
* otherwise **macOS → the login Keychain** (via the ``security`` CLI);
* otherwise, if the ``keyring`` package is importable, the **system keyring** (Secret Service /
  Windows Credential Manager);
* otherwise a ``0600`` JSON file at ``<config-home>/browser-secrets.json``.

The file backend is an **insecure fallback**: ``0600`` only stops *other local users* from reading it —
it is plaintext at rest, not encryption. :func:`has_secure_backend` reports whether a real OS store is
in play, so callers that persist cookies / credentials (storage-state) can refuse or warn when it is
not (see ``identity_store.export_identity_state``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_KEYCHAIN_SERVICE = "tabvis-browser-secret"
_BACKEND_ENV = "TABVIS_SECRET_BACKEND"
_lock = threading.RLock()
_keyring_available: bool | None = None
_warned_insecure = False


def new_secret_ref() -> str:
    return "sec_" + uuid.uuid4().hex[:16]


def _has_keyring() -> bool:
    global _keyring_available
    if _keyring_available is None:
        _keyring_available = importlib.util.find_spec("keyring") is not None
    return _keyring_available


def _resolve_backend() -> str:
    """Which backend to use: ``keychain`` | ``keyring`` | ``file`` (see module docstring)."""
    explicit = os.environ.get(_BACKEND_ENV, "").strip().lower()
    if explicit in ("file", "keychain", "keyring"):
        return explicit
    # Back-compat: the old opt-in still forces a secure backend when set.
    legacy = os.environ.get("TABVIS_BROWSER_SECRET_KEYCHAIN", "").strip().lower()
    force_secure = legacy in ("1", "true", "yes", "on")
    if sys.platform == "darwin":
        return "keychain"
    if _has_keyring():
        return "keyring"
    if force_secure:
        # Asked for a secure backend but none is available — warn and degrade rather than crash.
        _warn_insecure_once()
    return "file"


def has_secure_backend() -> bool:
    """Whether secrets are held in a real OS credential store (not the plaintext ``0600`` file)."""
    return _resolve_backend() in ("keychain", "keyring")


class InsecureSecretBackendError(RuntimeError):
    """Raised when production mode requires a secure backend but only the plaintext file is available.

    Production MUST fail closed rather than silently degrade to a plaintext ``0600`` JSON file
    (CREDENTIAL_INJECTION_DESIGN.md §6.1, §15 Phase 0, §17). The managed-authentication feature refuses
    to run rather than lower its security level.
    """


def _production_secure_backend_required() -> bool:
    """Whether the current config demands a secure secret backend (managed-auth production gate).

    True when either the broker runs in production mode (``TABVIS_CREDENTIAL_BROKER_MODE=production``)
    or the explicit requirement flag is set (``TABVIS_MANAGED_AUTH_REQUIRE_SECURE_SECRET_BACKEND=1``).
    Off by default so existing non-managed flows (and the test suite's pinned ``file`` backend) are
    unaffected.
    """
    mode = os.environ.get("TABVIS_CREDENTIAL_BROKER_MODE", "").strip().lower()
    if mode == "production":
        return True
    from tabvis.utils.env_utils import is_env_truthy

    return is_env_truthy(os.environ.get("TABVIS_MANAGED_AUTH_REQUIRE_SECURE_SECRET_BACKEND"))


def assert_production_backend() -> None:
    """Fail closed if a secure backend is required but not available (design §6.1, §17 startup check).

    Called by managed-authentication code paths before touching the secret store. In non-production
    configurations this is a no-op, so ordinary browser flows keep the best-effort behavior.
    """
    if _production_secure_backend_required() and not has_secure_backend():
        raise InsecureSecretBackendError(
            "managed authentication requires a secure OS secret backend (keychain/keyring); the "
            "plaintext file fallback is disabled in production mode."
        )


def _warn_insecure_once() -> None:
    global _warned_insecure
    if not _warned_insecure:
        _warned_insecure = True
        log_for_debugging(
            "[SECRET] no secure OS keystore available; using the plaintext 0600 file fallback. "
            "This is NOT encryption at rest — storage-state / credential persistence is discouraged."
        )


# --------------------------------------------------------------------------- file backend


def _file_path() -> str:
    return os.path.join(get_tabvis_config_home_dir(), "browser-secrets.json")


def _file_load() -> dict[str, str]:
    try:
        with open(_file_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _file_save(data: dict[str, str]) -> None:
    path = _file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- keychain backend


def _kc_set(ref: str, value: str) -> None:
    # NOTE: `security add-generic-password` takes the password as an argv element (`-w <value>`), so
    # it is briefly visible in `ps` for the duration of the call — a known limitation of the CLI (a
    # real fix needs a native Keychain binding). The value is NEVER logged (see put()'s except).
    subprocess.run(  # noqa: S603 - fixed `security` binary, controlled args
        ["security", "add-generic-password", "-U", "-a", ref, "-s", _KEYCHAIN_SERVICE, "-w", value],
        check=True,
        capture_output=True,
    )


def _kc_get(ref: str) -> str | None:
    result = subprocess.run(  # noqa: S603
        ["security", "find-generic-password", "-a", ref, "-s", _KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    # `security -w` appends exactly one trailing newline; strip only that — never the secret's own
    # leading/trailing whitespace, which .strip() would silently corrupt.
    out = result.stdout
    return out[:-1] if out.endswith("\n") else out


def _kc_delete(ref: str) -> None:
    subprocess.run(  # noqa: S603
        ["security", "delete-generic-password", "-a", ref, "-s", _KEYCHAIN_SERVICE],
        capture_output=True,
    )


# --------------------------------------------------------------------------- keyring backend


def _kr_set(ref: str, value: str) -> None:
    import keyring  # type: ignore[import-untyped]

    keyring.set_password(_KEYCHAIN_SERVICE, ref, value)


def _kr_get(ref: str) -> str | None:
    import keyring  # type: ignore[import-untyped]

    return keyring.get_password(_KEYCHAIN_SERVICE, ref)


def _kr_delete(ref: str) -> None:
    import keyring  # type: ignore[import-untyped]

    try:
        keyring.delete_password(_KEYCHAIN_SERVICE, ref)
    except Exception:  # noqa: BLE001 - deleting a missing entry raises on some backends
        pass


# --------------------------------------------------------------------------- public API


def _guard_backend(backend: str) -> None:
    """In production mode, refuse to touch the plaintext file backend at all (design §6.1)."""
    if backend == "file" and _production_secure_backend_required():
        raise InsecureSecretBackendError(
            "plaintext file secret backend is disabled in production mode."
        )


def put(value: str, *, ref: str | None = None) -> str:
    """Store a secret; returns its ``secret_ref``. Best-effort — never raises.

    Exception: in production mode a plaintext file backend fails closed with
    :class:`InsecureSecretBackendError` rather than silently persisting plaintext (design §6.1).
    """
    ref = ref or new_secret_ref()
    with _lock:
        backend = _resolve_backend()
        _guard_backend(backend)
        try:
            if backend == "keychain":
                _kc_set(ref, value)
            elif backend == "keyring":
                _kr_set(ref, value)
            else:
                data = _file_load()
                data[ref] = value
                _file_save(data)
        except Exception:  # noqa: BLE001 - NEVER log the exception here: on the keychain backend a
            # CalledProcessError's str() embeds the `-w <value>` argv (the plaintext secret).
            log_for_debugging("[SECRET] put failed")
    return ref


def get(ref: str | None) -> str | None:
    """Resolve a ``secret_ref`` to its value, or None. Best-effort."""
    if not ref:
        return None
    with _lock:
        backend = _resolve_backend()
        _guard_backend(backend)
        try:
            if backend == "keychain":
                return _kc_get(ref)
            if backend == "keyring":
                return _kr_get(ref)
            return _file_load().get(ref)
        except Exception:  # noqa: BLE001 - fixed message only, never the exception (see put)
            log_for_debugging("[SECRET] get failed")
            return None


def delete(ref: str) -> None:
    """Remove a secret. Best-effort."""
    if not ref:
        return
    with _lock:
        backend = _resolve_backend()
        _guard_backend(backend)
        try:
            if backend == "keychain":
                _kc_delete(ref)
            elif backend == "keyring":
                _kr_delete(ref)
            else:
                data = _file_load()
                data.pop(ref, None)
                _file_save(data)
        except Exception:  # noqa: BLE001 - fixed message only, never the exception (see put)
            log_for_debugging("[SECRET] delete failed")
