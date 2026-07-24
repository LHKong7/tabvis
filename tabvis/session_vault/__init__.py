"""Session Vault — task-isolated, envelope-encrypted authenticated sessions (design §10, Phase 4)."""

from __future__ import annotations

from tabvis.session_vault.crypto import (
    KeyProvider,
    LocalKeyProvider,
    SessionCryptoError,
    decrypt_storage_state,
    encrypt_storage_state,
)
from tabvis.session_vault.models import AuthenticatedSession
from tabvis.session_vault.store import SessionVault

__all__ = [
    "AuthenticatedSession",
    "KeyProvider",
    "LocalKeyProvider",
    "SessionCryptoError",
    "SessionVault",
    "decrypt_storage_state",
    "encrypt_storage_state",
]
