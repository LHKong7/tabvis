"""Production mode forbids the plaintext file secret backend (design §6.1, §15 Phase 0, §17).

The autouse suite fixture pins ``TABVIS_SECRET_BACKEND=file``, so these tests assert that turning on
production mode makes that plaintext backend fail closed rather than silently degrade.
"""

from __future__ import annotations

import pytest

from tabvis.browser import secret_store
from tabvis.browser.secret_store import InsecureSecretBackendError


def test_default_mode_file_backend_still_works() -> None:
    # no production flag → best-effort file backend, unchanged behavior
    ref = secret_store.put("value-1")
    assert secret_store.get(ref) == "value-1"
    secret_store.delete(ref)


def test_production_mode_blocks_put(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_CREDENTIAL_BROKER_MODE", "production")
    with pytest.raises(InsecureSecretBackendError):
        secret_store.put("value-2")


def test_production_mode_blocks_get_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_CREDENTIAL_BROKER_MODE", "production")
    with pytest.raises(InsecureSecretBackendError):
        secret_store.get("sec_x")
    with pytest.raises(InsecureSecretBackendError):
        secret_store.delete("sec_x")


def test_require_secure_backend_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_MANAGED_AUTH_REQUIRE_SECURE_SECRET_BACKEND", "1")
    with pytest.raises(InsecureSecretBackendError):
        secret_store.assert_production_backend()


def test_assert_production_backend_noop_without_flag() -> None:
    # off by default → no raise
    secret_store.assert_production_backend()
