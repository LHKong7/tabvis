"""Session Vault store — task-isolated, encrypted, TTL'd authenticated sessions (design §10).

Persists :class:`AuthenticatedSession` records as JSON sidecars under
``<config-home>/auth-sessions/<id>.json`` (ciphertext only). Enforces the lifecycle & reuse rules of
§10.3:

* by default a session is bound to its **source task** — a different task cannot open it, and
  :func:`end_task` deletes every non-reusable session created by that task;
* cross-task reuse requires **all** of: the profile permits it, the same user, a live TTL, and the
  requested origin set is within the session's allowed origins;
* an expired / revoked / profile-disabled session is **cascade-deleted** (record + envelope, and the
  DEK lives inside the envelope, so dropping the record drops the key material) (§10.3).

Decryption always goes through the AAD-binding crypto, so a record can only ever be opened with the
matching (user, task, profile, session) context (§10.2).
"""

from __future__ import annotations

import base64
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

from tabvis.session_vault.crypto import (
    KeyProvider,
    SessionCryptoError,
    decrypt_storage_state,
    encrypt_storage_state,
)
from tabvis.session_vault.models import AuthenticatedSession
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_DIRNAME = "auth-sessions"
_lock = threading.RLock()


def _dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), _DIRNAME)


def _path(session_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "-" for c in session_id)[:128]
    return os.path.join(_dir(), f"{safe or 'session'}.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class SessionVault:
    def __init__(self, key_provider: KeyProvider) -> None:
        self._keys = key_provider

    # ------------------------------------------------------------------ create

    def create(
        self,
        *,
        storage_state: dict,
        user_id: str,
        task_id: str,
        credential_profile_id: str,
        allowed_origins: list[str],
        ttl_seconds: int = 3600,
        reusable_across_tasks: bool = False,
    ) -> AuthenticatedSession:
        """Encrypt + persist a new authenticated session. Raises on crypto failure (never plaintext)."""
        session_id = "authsess_" + uuid.uuid4().hex[:16]
        envelope = encrypt_storage_state(
            storage_state,
            key_provider=self._keys,
            user_id=user_id,
            task_id=task_id,
            profile_id=credential_profile_id,
            session_id=session_id,
        )
        now = _now()
        session = AuthenticatedSession(
            id=session_id,
            owner_user_id=user_id,
            source_task_id=task_id,
            credential_profile_id=credential_profile_id,
            encrypted_storage_state=envelope,
            allowed_origins=list(allowed_origins),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            reusable_across_tasks=reusable_across_tasks,
            encryption_key_id=self._keys.key_id,
        )
        self._write(session)
        return session

    # ------------------------------------------------------------------ open (reuse)

    def open(
        self, session_id: str, *, user_id: str, task_id: str, requested_origins: list[str] | None = None
    ) -> dict | None:
        """Decrypt a session's storage state if the reuse policy allows it, else None (§10.3).

        Returns None (and cascade-deletes) when the session is missing, expired, owned by another user,
        or bound to a different task without cross-task reuse. Origin scope is enforced when
        ``requested_origins`` is given.
        """
        with _lock:
            session = self._read(session_id)
            if session is None:
                return None
            # expired → cascade delete
            if _now() >= _as_aware(session.expires_at):
                self._delete(session_id)
                return None
            if session.owner_user_id != user_id:
                return None
            if task_id != session.source_task_id and not session.reusable_across_tasks:
                return None
            if requested_origins is not None and not set(requested_origins).issubset(
                set(session.allowed_origins)
            ):
                return None
        # decrypt with the ORIGINAL (source-task) AAD — reuse restores the same session, it does not
        # re-key per opening task (§10.3 "复用只恢复到同一用户和允许 Origin 集合").
        try:
            return decrypt_storage_state(
                session.encrypted_storage_state,
                key_provider=self._keys,
                user_id=session.owner_user_id,
                task_id=session.source_task_id,
                profile_id=session.credential_profile_id,
                session_id=session.id,
            )
        except SessionCryptoError:
            return None

    # ------------------------------------------------------------------ lifecycle

    def delete(self, session_id: str) -> bool:
        with _lock:
            return self._delete(session_id)

    def end_task(self, task_id: str) -> int:
        """Delete every non-reusable session created by a finished task (§10.3). Returns count."""
        removed = 0
        with _lock:
            for session in self._all():
                if session.source_task_id == task_id and not session.reusable_across_tasks:
                    if self._delete(session.id):
                        removed += 1
        return removed

    def revoke_for_profile(self, credential_profile_id: str) -> int:
        """Cascade-delete every session for a profile (profile disabled / user revoked, §10.3)."""
        removed = 0
        with _lock:
            for session in self._all():
                if session.credential_profile_id == credential_profile_id:
                    if self._delete(session.id):
                        removed += 1
        return removed

    def purge_expired(self) -> int:
        removed = 0
        now = _now()
        with _lock:
            for session in self._all():
                if now >= _as_aware(session.expires_at):
                    if self._delete(session.id):
                        removed += 1
        return removed

    # ------------------------------------------------------------------ persistence

    def _write(self, session: AuthenticatedSession) -> None:
        os.makedirs(_dir(), exist_ok=True)
        data = session.model_dump(mode="json")
        # bytes → base64 str for JSON (model_dump(mode=json) would lossily utf-8-decode raw bytes)
        data["encrypted_storage_state"] = base64.b64encode(session.encrypted_storage_state).decode(
            "ascii"
        )
        path = _path(session.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)

    def _read(self, session_id: str) -> AuthenticatedSession | None:
        try:
            with open(_path(session_id), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        try:
            data["encrypted_storage_state"] = base64.b64decode(data["encrypted_storage_state"])
            return AuthenticatedSession.model_validate(data)
        except Exception:  # noqa: BLE001 - a corrupt record is treated as absent
            return None

    def _all(self) -> list[AuthenticatedSession]:
        try:
            names = os.listdir(_dir())
        except OSError:
            return []
        out: list[AuthenticatedSession] = []
        for name in names:
            if not name.endswith(".json") or name.endswith(".tmp"):
                continue
            session = self._read(name[: -len(".json")])
            if session is not None:
                out.append(session)
        return out

    def _delete(self, session_id: str) -> bool:
        try:
            os.remove(_path(session_id))
            return True
        except OSError:
            return False
