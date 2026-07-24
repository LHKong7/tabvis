"""Authenticated session model (design §10.1)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuthenticatedSession(BaseModel):
    """A stored, encrypted authenticated session (design §10.1).

    ``encrypted_storage_state`` is the serialized envelope from
    :mod:`tabvis.session_vault.crypto` — never plaintext cookies. The record carries the metadata the
    reuse policy needs (owner, source task, profile, allowed origins, TTL, reuse flag, key id) but the
    cookies themselves are only recoverable by decrypting with the matching (user, task, profile,
    session) AAD and the KEK.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    owner_user_id: str
    source_task_id: str
    credential_profile_id: str
    encrypted_storage_state: bytes
    allowed_origins: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    reusable_across_tasks: bool = False
    encryption_key_id: str
