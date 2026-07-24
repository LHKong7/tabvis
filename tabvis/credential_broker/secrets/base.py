"""Secret Provider protocol (design §6.1).

A provider resolves a ``secret_ref`` to a :class:`~tabvis.authentication.secrets.SecretValue`. It lives
only in the Broker's permission domain (design §4.1): the Agent process has no path to it. Providers
MUST fail safe when unavailable (design §6.1) and MUST return a :class:`SecretValue`, never a plain
``str``, so a resolved secret is non-serializable from the moment it exists.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tabvis.authentication.secrets import SecretValue


class SecretProviderUnavailable(RuntimeError):
    """The backing secret system is unreachable/unhealthy. Callers map this to a stable error code."""


@runtime_checkable
class SecretProvider(Protocol):
    async def resolve(self, secret_ref: str) -> SecretValue:
        """Resolve a reference to a secret. Raises :class:`SecretProviderUnavailable` if it cannot."""
        ...

    async def health(self) -> bool:
        """Whether the provider is currently usable."""
        ...
