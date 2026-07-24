"""OS-keystore secret providers (design §6.1).

These wrap the existing :mod:`tabvis.browser.secret_store` OS backends (macOS Keychain / system
keyring) but return a :class:`~tabvis.authentication.secrets.SecretValue` instead of a plain ``str`` —
the plaintext is wrapped into an overwritable buffer the instant it is read, and the intermediate
``str`` returned by the backend is dropped immediately.

Production hardening note (§6.1): the native keychain path MUST NOT pass secrets as command-line
arguments (they show up in ``ps``). The current ``secret_store`` keychain backend uses the ``security``
CLI on *write*; these providers only ever *read* (``find-generic-password -w`` takes no secret argv),
so the read path is safe. The write-side CLI limitation is tracked for the native-binding migration and
does not affect resolution here.
"""

from __future__ import annotations

from tabvis.authentication.secrets import SecretValue, secret_from_str
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable


class NativeKeychainProvider:
    """Reads secrets from the OS keystore (macOS Keychain / system keyring) via ``secret_store``."""

    async def resolve(self, secret_ref: str) -> SecretValue:
        from tabvis.browser import secret_store

        if not secret_store.has_secure_backend():
            # Production forbids the plaintext file fallback (design §6.1); fail safe rather than read it.
            raise SecretProviderUnavailable("no secure OS keystore available")
        plaintext = secret_store.get(secret_ref)
        if plaintext is None:
            raise SecretProviderUnavailable("secret not found for ref")
        try:
            return secret_from_str(plaintext)
        finally:
            del plaintext  # drop our reference to the intermediate str immediately

    async def health(self) -> bool:
        from tabvis.browser import secret_store

        return secret_store.has_secure_backend()


class KeyringProvider(NativeKeychainProvider):
    """System-keyring provider. Same read path as the keychain provider (backend picked by env)."""
