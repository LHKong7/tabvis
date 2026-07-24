"""Secret providers (§6.1) and Broker hardening (§5.7, §17)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.authentication.secrets import SecretLeakError
from tabvis.credential_broker import hardening
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable
from tabvis.credential_broker.secrets.memory import MemorySecretProvider
from tabvis.credential_broker.secrets.vault import VaultProvider


def _run(coro):
    return asyncio.run(coro)


def test_memory_provider_returns_secret_value() -> None:
    provider = MemorySecretProvider({"sec_1": "hunter2xyz"})
    value = _run(provider.resolve("sec_1"))
    assert bytes(value.borrow_bytes()) == b"hunter2xyz"
    with pytest.raises(SecretLeakError):
        str(value)  # provider output is a non-serializable SecretValue
    value.release()


def test_memory_provider_missing_ref() -> None:
    provider = MemorySecretProvider({})
    with pytest.raises(SecretProviderUnavailable):
        _run(provider.resolve("sec_missing"))


def test_vault_provider_via_injected_fetch() -> None:
    async def fetch(ref: str):
        return {"sec_pw": "s3cr3tvalue"}.get(ref)

    provider = VaultProvider(fetch)
    value = _run(provider.resolve("sec_pw"))
    assert bytes(value.borrow_bytes()) == b"s3cr3tvalue"
    value.release()
    with pytest.raises(SecretProviderUnavailable):
        _run(provider.resolve("sec_unknown"))


def test_vault_provider_transport_error_is_unavailable() -> None:
    async def fetch(ref: str):
        raise ConnectionError("vault down")

    provider = VaultProvider(fetch)
    with pytest.raises(SecretProviderUnavailable):
        _run(provider.resolve("sec_pw"))


def test_scrub_secret_env_removes_secret_vars_keeps_config() -> None:
    env = {
        "MY_API_TOKEN": "abc",
        "DB_PASSWORD": "def",
        "SOME_SECRET": "ghi",
        "TABVIS_SECRET_BACKEND": "keychain",  # kept — a config reference, not plaintext
        "PATH": "/usr/bin",
    }
    removed = hardening.scrub_secret_env(env)
    assert set(removed) == {"MY_API_TOKEN", "DB_PASSWORD", "SOME_SECRET"}
    assert env == {"TABVIS_SECRET_BACKEND": "keychain", "PATH": "/usr/bin"}


def test_disable_core_dumps_returns_bool() -> None:
    # returns True on POSIX with resource module; never raises
    assert isinstance(hardening.disable_core_dumps(), bool)


def test_apply_startup_hardening_summary_has_no_secrets() -> None:
    summary = hardening.apply_startup_hardening()
    assert set(summary) == {"core_dumps_disabled", "scrubbed_env_count"}
