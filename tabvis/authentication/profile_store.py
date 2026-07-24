"""Credential Profile store — references only, never plaintext (design §5.4, §14).

Persists :class:`~tabvis.authentication.models.CredentialProfile` records as JSON sidecars under
``<config-home>/credential-profiles/<id>.json``, mirroring the identity-store pattern already used in
this repo. The security invariants (design §5.4):

* a profile holds only ``*_secret_ref`` references — the model itself has no plaintext field, so the
  store *cannot* persist a secret even by mistake;
* the profile id is not a secret and is not an authorization on its own — every load re-checks
  ``owner_user_id`` against the requesting principal (:func:`get_for_user`);
* ``allowed_origins`` are validated to canonical exact Origins by the model on load, so a tampered
  sidecar with a wildcard / path origin fails closed.
"""

from __future__ import annotations

import json
import os
import re
import threading

from tabvis.authentication.models import CredentialProfile
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir

_PROFILES_DIRNAME = "credential-profiles"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_lock = threading.RLock()


def profiles_dir() -> str:
    return os.path.join(get_tabvis_config_home_dir(), _PROFILES_DIRNAME)


def _slug(profile_id: str) -> str:
    slug = _SLUG_RE.sub("-", profile_id.strip()).strip(".-")
    return slug[:128] or "profile"


def _path(profile_id: str) -> str:
    return os.path.join(profiles_dir(), f"{_slug(profile_id)}.json")


def put(profile: CredentialProfile) -> None:
    """Persist a profile (JSON sidecar). Overwrites any existing record with the same id."""
    with _lock:
        os.makedirs(profiles_dir(), exist_ok=True)
        path = _path(profile.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(profile.model_dump(mode="json"), fh, indent=2)
        os.replace(tmp, path)


def get(profile_id: str) -> CredentialProfile | None:
    """Load a profile by id, or None. Returns None (never raises) on a corrupt / invalid sidecar."""
    with _lock:
        try:
            with open(_path(profile_id), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
    try:
        return CredentialProfile.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - a tampered/invalid profile fails closed, not open
        log_for_debugging(f"[CRED] profile {profile_id!r} failed validation: {type(exc).__name__}")
        return None


def get_for_user(profile_id: str, user_id: str) -> CredentialProfile | None:
    """Load a profile only if ``user_id`` owns it (design §5.4 "每次使用 MUST 验证 owner_user_id").

    Ownership is re-checked on every use — the profile id alone never authorizes access. Returns None
    both when the profile is missing and when it is owned by someone else, so a caller cannot probe for
    the existence of another user's profile.
    """
    profile = get(profile_id)
    if profile is None or profile.owner_user_id != user_id:
        return None
    return profile


def delete(profile_id: str) -> bool:
    """Remove a profile sidecar. Returns whether one existed. Does not touch the referenced secrets."""
    with _lock:
        try:
            os.remove(_path(profile_id))
            return True
        except OSError:
            return False
