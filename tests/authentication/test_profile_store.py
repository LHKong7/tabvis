"""Credential profile store — references only, ownership re-checked (design §5.4, §16.1)."""

from __future__ import annotations

from tabvis.authentication import profile_store
from tabvis.authentication.models import CredentialProfile


def _profile(**overrides) -> CredentialProfile:
    base = dict(
        id="work_sso",
        owner_user_id="u1",
        allowed_origins=["https://accounts.example.com"],
        authentication_adapter="generic_password_v1",
        password_secret_ref="sec_abc",
    )
    base.update(overrides)
    return CredentialProfile(**base)


def test_round_trip() -> None:
    profile_store.put(_profile())
    loaded = profile_store.get("work_sso")
    assert loaded is not None
    assert loaded.owner_user_id == "u1"
    assert loaded.password_secret_ref == "sec_abc"
    assert loaded.allowed_origins == ["https://accounts.example.com"]


def test_get_for_user_enforces_ownership() -> None:
    profile_store.put(_profile(owner_user_id="u1"))
    assert profile_store.get_for_user("work_sso", "u1") is not None
    # another user cannot even probe existence — same None as a missing profile
    assert profile_store.get_for_user("work_sso", "u2") is None


def test_missing_profile_is_none() -> None:
    assert profile_store.get("nope") is None
    assert profile_store.get_for_user("nope", "u1") is None


def test_stored_json_holds_only_refs(tmp_path, config_home) -> None:
    import json
    import os

    profile_store.put(_profile(password_secret_ref="sec_only_a_ref"))
    path = os.path.join(profile_store.profiles_dir(), "work_sso.json")
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    # the persisted record carries the ref, never a plaintext credential field
    assert raw["password_secret_ref"] == "sec_only_a_ref"
    assert "password" not in raw and "username" not in raw


def test_delete() -> None:
    profile_store.put(_profile())
    assert profile_store.delete("work_sso") is True
    assert profile_store.get("work_sso") is None
    assert profile_store.delete("work_sso") is False
