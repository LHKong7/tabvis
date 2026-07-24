"""Session Vault store: task isolation, reuse policy, cascade delete (design §10.3, §16.2)."""

from __future__ import annotations

import os

from tabvis.session_vault.crypto import LocalKeyProvider
from tabvis.session_vault.store import SessionVault

_STATE = {"cookies": [{"name": "sid", "value": "abc123"}]}


def _vault() -> SessionVault:
    return SessionVault(LocalKeyProvider(os.urandom(32)))


def _create(vault, **overrides):
    base = dict(
        storage_state=_STATE,
        user_id="u1",
        task_id="t1",
        credential_profile_id="p1",
        allowed_origins=["https://accounts.example.com"],
    )
    base.update(overrides)
    return vault.create(**base)


def test_create_persists_ciphertext_only() -> None:
    vault = _vault()
    session = _create(vault)
    # the on-disk record must not contain the plaintext cookie value
    from tabvis.session_vault.store import _path

    with open(_path(session.id), "rb") as fh:
        raw = fh.read()
    assert b"abc123" not in raw


def test_open_in_source_task() -> None:
    vault = _vault()
    session = _create(vault)
    out = vault.open(session.id, user_id="u1", task_id="t1")
    assert out == _STATE


def test_other_task_cannot_open_non_reusable() -> None:
    vault = _vault()
    session = _create(vault, reusable_across_tasks=False)
    assert vault.open(session.id, user_id="u1", task_id="t2") is None


def test_reusable_across_tasks() -> None:
    vault = _vault()
    session = _create(vault, reusable_across_tasks=True)
    assert vault.open(session.id, user_id="u1", task_id="t2") == _STATE


def test_other_user_cannot_open() -> None:
    vault = _vault()
    session = _create(vault, reusable_across_tasks=True)
    assert vault.open(session.id, user_id="intruder", task_id="t1") is None


def test_origin_scope_enforced() -> None:
    vault = _vault()
    session = _create(vault, reusable_across_tasks=True)
    assert vault.open(
        session.id, user_id="u1", task_id="t1", requested_origins=["https://accounts.example.com"]
    ) == _STATE
    # an origin outside the session's allowed set is refused
    assert (
        vault.open(session.id, user_id="u1", task_id="t1", requested_origins=["https://other.test"])
        is None
    )


def test_expired_session_cascade_deleted_on_open() -> None:
    vault = _vault()
    session = _create(vault, ttl_seconds=-1)  # already expired
    from tabvis.session_vault.store import _path

    assert os.path.exists(_path(session.id))
    assert vault.open(session.id, user_id="u1", task_id="t1") is None
    assert not os.path.exists(_path(session.id))  # cascade deleted


def test_end_task_deletes_non_reusable_only() -> None:
    vault = _vault()
    a = _create(vault, task_id="t1", reusable_across_tasks=False)
    b = _create(vault, task_id="t1", reusable_across_tasks=True)
    removed = vault.end_task("t1")
    assert removed == 1
    assert vault.open(a.id, user_id="u1", task_id="t1") is None
    assert vault.open(b.id, user_id="u1", task_id="t1") == _STATE


def test_revoke_for_profile_cascades() -> None:
    vault = _vault()
    a = _create(vault, credential_profile_id="p1")
    b = _create(vault, credential_profile_id="p2")
    assert vault.revoke_for_profile("p1") == 1
    assert vault.open(a.id, user_id="u1", task_id="t1") is None
    assert vault.open(b.id, user_id="u1", task_id="t1") == _STATE


def test_purge_expired() -> None:
    vault = _vault()
    _create(vault, ttl_seconds=-1)
    live = _create(vault, ttl_seconds=3600)
    assert vault.purge_expired() == 1
    assert vault.open(live.id, user_id="u1", task_id="t1") == _STATE
