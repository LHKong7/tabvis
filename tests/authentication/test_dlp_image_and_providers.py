"""Screenshot DLP policy (§11.2) and external secret providers (§6.1)."""

from __future__ import annotations

import asyncio

import pytest

from tabvis.authentication.secrets import SecretLeakError
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable
from tabvis.credential_broker.secrets.onepassword import OnePasswordProvider
from tabvis.dlp.image import capture_allowed, post_auth_redaction_spec


def _run(coro):
    return asyncio.run(coro)


def test_capture_forbidden_during_authentication() -> None:
    assert capture_allowed(authentication_in_progress=True) is False
    assert capture_allowed(authentication_in_progress=False) is True


def test_post_auth_redaction_masks_sensitive_fields() -> None:
    spec = post_auth_redaction_spec()
    assert "password" in spec.mask_field_roles
    assert "totp" in spec.mask_field_roles
    assert "username" in spec.mask_field_roles


def test_onepassword_provider_resolves() -> None:
    async def fetch(ref: str):
        return {"op://vault/item/pw": "s3cr3tvalue"}.get(ref)

    provider = OnePasswordProvider(fetch)
    value = _run(provider.resolve("op://vault/item/pw"))
    assert bytes(value.borrow_bytes()) == b"s3cr3tvalue"
    with pytest.raises(SecretLeakError):
        str(value)
    value.release()


def test_onepassword_missing_and_error() -> None:
    async def fetch_missing(ref: str):
        return None

    async def fetch_error(ref: str):
        raise ConnectionError("op down")

    with pytest.raises(SecretProviderUnavailable):
        _run(OnePasswordProvider(fetch_missing).resolve("op://x"))
    with pytest.raises(SecretProviderUnavailable):
        _run(OnePasswordProvider(fetch_error).resolve("op://x"))
