"""Browser secret store (IDP-6) — ``secret_ref`` indirection over the OS Keychain or a 0600 file.

``design.md`` §"数据存储": passwords / tokens / proxy keys and the data-encryption key live in the OS
Keychain; the DB and records keep only a ``secret_ref``. This is that store: :func:`put` returns an
opaque ref, :func:`get` resolves it, so an identity/record never holds the plaintext.

Backend: the macOS Keychain (via the ``security`` CLI) when ``TABVIS_BROWSER_SECRET_KEYCHAIN`` is set —
opt-in so tests and non-macOS hosts use the file backend and never touch the real keychain; otherwise
a ``0600`` JSON file at ``<config-home>/browser-secrets.json``. Nothing calls this by default, so it
is purely additive. (Keychain-by-default is the north-star; the file fallback carries the same
posture as today's ``0600`` ``.env``.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_KEYCHAIN_SERVICE = "tabvis-browser-secret"
_lock = threading.RLock()


def new_secret_ref() -> str:
    return "sec_" + uuid.uuid4().hex[:16]


def _use_keychain() -> bool:
    if sys.platform != "darwin":
        return False
    val = os.environ.get("TABVIS_BROWSER_SECRET_KEYCHAIN", "")
    return val.strip().lower() in ("1", "true", "yes", "on")


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


# --------------------------------------------------------------------------- public API


def put(value: str, *, ref: str | None = None) -> str:
    """Store a secret; returns its ``secret_ref``. Best-effort — never raises."""
    ref = ref or new_secret_ref()
    with _lock:
        try:
            if _use_keychain():
                _kc_set(ref, value)
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
        try:
            return _kc_get(ref) if _use_keychain() else _file_load().get(ref)
        except Exception:  # noqa: BLE001 - fixed message only, never the exception (see put)
            log_for_debugging("[SECRET] get failed")
            return None


def delete(ref: str) -> None:
    """Remove a secret. Best-effort."""
    if not ref:
        return
    with _lock:
        try:
            if _use_keychain():
                _kc_delete(ref)
            else:
                data = _file_load()
                data.pop(ref, None)
                _file_save(data)
        except Exception:  # noqa: BLE001 - fixed message only, never the exception (see put)
            log_for_debugging("[SECRET] delete failed")
