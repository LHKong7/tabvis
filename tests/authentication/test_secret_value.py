"""SecretValue / ResolvedCredentials contract (design §5.7, §5.8, §16.1).

The whole point of the type is that a resolved secret cannot be *accidentally* rendered or serialized.
Every leak path must raise, and the one sanctioned path (borrow/release) must work and then scrub.
"""

from __future__ import annotations

import copy
import json
import pickle

import pytest

from tabvis.authentication.secrets import (
    BufferSecretValue,
    SecretLeakError,
    SecretValue,
    secret_from_str,
)


def test_borrow_returns_bytes_then_release_scrubs() -> None:
    s = secret_from_str("p@ssw0rd")
    assert bytes(s.borrow_bytes()) == b"p@ssw0rd"
    s.release()
    with pytest.raises(SecretLeakError):
        s.borrow_bytes()


def test_release_is_idempotent() -> None:
    s = secret_from_str("x" * 12)
    s.release()
    s.release()  # no raise


def test_borrowed_view_is_readonly() -> None:
    s = secret_from_str("secret-value")
    view = s.borrow_bytes()
    with pytest.raises((TypeError, ValueError)):
        view[0] = 0  # type: ignore[index]


def test_str_raises() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises(SecretLeakError):
        str(s)


def test_format_and_fstring_raise() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises(SecretLeakError):
        "{}".format(s)
    with pytest.raises(SecretLeakError):
        _ = f"{s}"


def test_repr_is_redacted_and_never_contains_secret() -> None:
    s = secret_from_str("topsecret123")
    assert repr(s) == "<SecretValue redacted>"
    assert "topsecret123" not in repr(s)


def test_bytes_dunder_raises() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises(SecretLeakError):
        s.__bytes__()


def test_pickle_raises() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises((SecretLeakError, pickle.PicklingError)):
        pickle.dumps(s)


def test_copy_and_deepcopy_raise() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises(SecretLeakError):
        copy.copy(s)
    with pytest.raises(SecretLeakError):
        copy.deepcopy(s)


def test_json_dumps_cannot_serialize() -> None:
    s = secret_from_str("hunter2")
    with pytest.raises(TypeError):
        json.dumps(s)  # not JSON-serializable at all
    with pytest.raises(SecretLeakError):
        json.dumps({"pw": s}, default=str)  # default=str hits __str__ which raises


def test_isinstance_protocol() -> None:
    assert isinstance(secret_from_str("abcdef"), SecretValue)


def test_resolved_credentials_release_all() -> None:
    from tabvis.authentication.models import ResolvedCredentials

    rc = ResolvedCredentials(
        username=secret_from_str("alice@example.com"),
        password=secret_from_str("hunter2xyz"),
        totp_seed=secret_from_str("JBSWY3DPEHPK3PXP"),
    )
    rc.release()
    for slot in (rc.username, rc.password, rc.totp_seed):
        assert isinstance(slot, BufferSecretValue)
        with pytest.raises(SecretLeakError):
            slot.borrow_bytes()


def test_resolved_credentials_not_serializable() -> None:
    from tabvis.authentication.models import ResolvedCredentials

    rc = ResolvedCredentials(password=secret_from_str("hunter2xyz"))
    with pytest.raises(SecretLeakError):
        str(rc)
    with pytest.raises(SecretLeakError):
        pickle.dumps(rc)
    assert repr(rc) == "<ResolvedCredentials redacted>"
