"""Credential Broker — trusted-domain authentication process (design §4, §6.2, Phase 2).

Holds the Broker orchestration (:mod:`broker`), the short-lived Executor that alone creates/destroys
:class:`~tabvis.authentication.models.ResolvedCredentials` (:mod:`executor`), the Secret Providers
(:mod:`secrets`), the process hardening (:mod:`hardening`), and the IPC server (:mod:`server`) that
lets the Broker run in a separate process from the Agent runtime (L1/L2, §2.3).
"""

from __future__ import annotations

from tabvis.credential_broker.broker import CredentialBroker, new_request_id
from tabvis.credential_broker.executor import CredentialExecutor
from tabvis.credential_broker.secrets.base import SecretProvider, SecretProviderUnavailable

__all__ = [
    "CredentialBroker",
    "CredentialExecutor",
    "SecretProvider",
    "SecretProviderUnavailable",
    "new_request_id",
]
