"""In-memory secret provider — development / tests only (design §2.3 L0).

NOT FOR PRODUCTION. Holds plaintext in a process dict so tests and the L0 same-process prototype can
resolve refs without a real keystore. It is deliberately in its own module (never imported by the
production provider-selection path) so it cannot be reached by accident.
"""

from __future__ import annotations

from tabvis.authentication.secrets import SecretValue, secret_from_str
from tabvis.credential_broker.secrets.base import SecretProviderUnavailable


class MemorySecretProvider:
    def __init__(self, secrets: dict[str, str] | None = None, *, healthy: bool = True) -> None:
        self._secrets = dict(secrets or {})
        self._healthy = healthy

    def put(self, secret_ref: str, value: str) -> None:
        self._secrets[secret_ref] = value

    async def resolve(self, secret_ref: str) -> SecretValue:
        if not self._healthy:
            raise SecretProviderUnavailable("provider marked unhealthy")
        if secret_ref not in self._secrets:
            raise SecretProviderUnavailable("secret not found for ref")
        return secret_from_str(self._secrets[secret_ref])

    async def health(self) -> bool:
        return self._healthy
