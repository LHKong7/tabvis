"""Secret Providers (design §6.1). Only reachable inside the Broker permission domain (§4.1)."""

from __future__ import annotations

from tabvis.credential_broker.secrets.base import SecretProvider, SecretProviderUnavailable
from tabvis.credential_broker.secrets.keychain import KeyringProvider, NativeKeychainProvider
from tabvis.credential_broker.secrets.onepassword import OnePasswordProvider
from tabvis.credential_broker.secrets.vault import VaultProvider

__all__ = [
    "KeyringProvider",
    "NativeKeychainProvider",
    "OnePasswordProvider",
    "SecretProvider",
    "SecretProviderUnavailable",
    "VaultProvider",
]
