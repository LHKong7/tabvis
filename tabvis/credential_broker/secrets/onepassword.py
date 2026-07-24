"""1Password secret provider (design §6.1, Phase 5 external providers).

Like :class:`~tabvis.credential_broker.secrets.vault.VaultProvider`, this is decoupled from the
concrete client: it takes a ``fetch`` coroutine that resolves an item reference (e.g. an
``op://vault/item/field`` secret reference) to plaintext via the 1Password Connect API or CLI. Keeping
the transport injected makes the provider unit-testable and keeps 1Password auth out of this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from tabvis.authentication.secrets import SecretValue, secret_from_str
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable

FetchFn = Callable[[str], Awaitable[str | None]]
HealthFn = Callable[[], Awaitable[bool]]


class OnePasswordProvider:
    def __init__(self, fetch: FetchFn, *, health: HealthFn | None = None) -> None:
        self._fetch = fetch
        self._health = health

    async def resolve(self, secret_ref: str) -> SecretValue:
        try:
            plaintext = await self._fetch(secret_ref)
        except Exception:  # noqa: BLE001 - any transport error is "unavailable", never a leak
            raise SecretProviderUnavailable("1password fetch failed") from None
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
