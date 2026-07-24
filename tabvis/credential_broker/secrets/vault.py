"""HashiCorp Vault secret provider (design §6.1, Phase 5 external providers).

The provider is decoupled from any concrete HTTP client: it takes a ``fetch`` coroutine that maps a
``secret_ref`` to plaintext (the caller wires it to a real Vault KV read with a workload token). This
keeps the provider unit-testable without a live Vault and keeps Vault-auth concerns out of this module.
The resolved plaintext is wrapped into a :class:`SecretValue` and the intermediate ``str`` is dropped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tabvis.authentication.secrets import SecretValue, secret_from_str
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable

FetchFn = Callable[[str], Awaitable[str | None]]
HealthFn = Callable[[], Awaitable[bool]]


class VaultProvider:
    def __init__(self, fetch: FetchFn, *, health: HealthFn | None = None) -> None:
        self._fetch = fetch
        self._health = health

    async def resolve(self, secret_ref: str) -> SecretValue:
        try:
            plaintext = await self._fetch(secret_ref)
        except Exception as exc:  # noqa: BLE001 - any transport error is "unavailable", not a leak
            raise SecretProviderUnavailable("vault fetch failed") from None
        if plaintext is None:
            raise SecretProviderUnavailable("secret not found for ref")
        try:
            return secret_from_str(plaintext)
        finally:
            del plaintext

    async def health(self) -> bool:
        if self._health is None:
            return True
        try:
            return await self._health()
        except Exception:  # noqa: BLE001
            return False
