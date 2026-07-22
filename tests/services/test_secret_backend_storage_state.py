"""Secure secret backend (issue #6) + explicit storage-state export/import & cascade (issues #6/#7).

The suite pins ``TABVIS_SECRET_BACKEND=file`` globally (tests/conftest); these tests flip it per case
to exercise the secure-backend gating without touching a real OS keystore.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from tabvis.browser import identity_store, secret_store


@pytest.fixture(autouse=True)
def _clean() -> Any:
    identity_store._cache.clear()
    secret_store._keyring_available = None
    yield
    identity_store._cache.clear()
    secret_store._keyring_available = None


# --------------------------------------------------------------------------- backend selection


def test_file_backend_is_not_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "file")
    assert secret_store.has_secure_backend() is False


def test_keychain_backend_is_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "keychain")
    assert secret_store.has_secure_backend() is True


def test_keyring_backend_is_secure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "keyring")
    assert secret_store.has_secure_backend() is True


def test_macos_defaults_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_SECRET_BACKEND", raising=False)
    monkeypatch.setattr(secret_store.sys, "platform", "darwin")
    assert secret_store._resolve_backend() == "keychain"
    assert secret_store.has_secure_backend() is True


def test_linux_uses_keyring_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_SECRET_BACKEND", raising=False)
    monkeypatch.setattr(secret_store.sys, "platform", "linux")
    monkeypatch.setattr(secret_store, "_has_keyring", lambda: True)
    assert secret_store._resolve_backend() == "keyring"


def test_linux_falls_back_to_file_without_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABVIS_SECRET_BACKEND", raising=False)
    monkeypatch.setattr(secret_store.sys, "platform", "linux")
    monkeypatch.setattr(secret_store, "_has_keyring", lambda: False)
    assert secret_store._resolve_backend() == "file"
    assert secret_store.has_secure_backend() is False


# --------------------------------------------------------------------------- storage-state export/import


def test_export_requires_browser_closed() -> None:
    with pytest.raises(ValueError, match="browser to be closed"):
        identity_store.export_identity_state(
            "ag1", {"cookies": []}, browser_closed=False, allow_insecure=True
        )


def test_export_requires_authorization() -> None:
    with pytest.raises(PermissionError):
        identity_store.export_identity_state(
            "ag1", {"cookies": []}, browser_closed=True, authorized=False, allow_insecure=True
        )


def test_export_refuses_insecure_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "file")  # not secure
    with pytest.raises(RuntimeError, match="no secure secret backend"):
        identity_store.export_identity_state("ag1", {"cookies": []}, browser_closed=True)


def test_export_allowed_with_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "file")
    meta = identity_store.export_identity_state(
        "ag1", {"cookies": [{"name": "sid", "value": "v"}]}, browser_closed=True, allow_insecure=True
    )
    assert meta["version"] == identity_store._STORAGE_STATE_VERSION and meta["exported_at"]


def test_export_allowed_with_secure_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "keychain")
    monkeypatch.setattr(secret_store, "_kc_set", lambda ref, value: None)
    # get resolves via keychain — return the envelope so import round-trips
    saved: dict[str, str] = {}
    monkeypatch.setattr(secret_store, "_kc_set", lambda ref, value: saved.__setitem__(ref, value))
    monkeypatch.setattr(secret_store, "_kc_get", lambda ref: saved.get(ref))
    meta = identity_store.export_identity_state(
        "ag_kc", {"cookies": [{"name": "sid", "value": "v"}]}, browser_closed=True
    )
    imported = identity_store.import_identity_state("ag_kc")
    assert imported["storage_state"] == {"cookies": [{"name": "sid", "value": "v"}]}
    assert imported["version"] == meta["version"] and imported["exported_at"] == meta["exported_at"]


def test_import_roundtrips_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "file")
    identity_store.export_identity_state(
        "ag2", {"cookies": [{"name": "a", "value": "b"}]}, browser_closed=True, allow_insecure=True
    )
    imported = identity_store.import_identity_state("ag2")
    assert imported["storage_state"] == {"cookies": [{"name": "a", "value": "b"}]}
    assert imported["version"] == 1 and imported["exported_at"]
    # low-level load unwraps the envelope too
    assert identity_store.load_storage_state("ag2") == {"cookies": [{"name": "a", "value": "b"}]}


def test_import_none_when_absent() -> None:
    assert identity_store.import_identity_state("ag_never") is None


# --------------------------------------------------------------------------- deletion cascade


def test_delete_identity_removes_secrets_and_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_SECRET_BACKEND", "file")
    cred_ref = identity_store.store_credential("ag_del", "s3cret")
    proxy_ref = identity_store.set_proxy("ag_del", "http://proxy.local:8080")
    identity_store.export_identity_state(
        "ag_del", {"cookies": []}, browser_closed=True, allow_insecure=True
    )
    ss_ref = identity_store.get_by_agent("ag_del").auth.storage_state_ref
    sidecar = identity_store._path("ag_del")
    assert os.path.exists(sidecar)
    assert secret_store.get(cred_ref) == "s3cret"

    assert identity_store.delete_identity("ag_del") is True

    assert secret_store.get(cred_ref) is None
    assert secret_store.get(proxy_ref) is None
    assert secret_store.get(ss_ref) is None
    assert not os.path.exists(sidecar)
    assert identity_store.get_by_agent("ag_del") is None


def test_delete_identity_absent_is_false() -> None:
    assert identity_store.delete_identity("ag_ghost") is False
