"""Static adapter registry (design §9.1)."""

from __future__ import annotations

import pytest

from tabvis.authentication.adapters.generic_password import GenericPasswordAdapter
from tabvis.authentication.adapters.registry import (
    get_adapter,
    is_registered_adapter,
    registered_adapter_names,
)


def test_generic_adapter_is_registered_and_versioned() -> None:
    assert is_registered_adapter("generic_password_v1")
    assert all(name[-1].isdigit() for name in registered_adapter_names())  # version-suffixed


def test_get_returns_fresh_instance() -> None:
    a = get_adapter("generic_password_v1")
    b = get_adapter("generic_password_v1")
    assert isinstance(a, GenericPasswordAdapter)
    assert a is not b  # stateless, per-use instance


def test_unknown_adapter_fails_closed() -> None:
    assert not is_registered_adapter("../../evil.py")
    with pytest.raises(KeyError):
        get_adapter("arbitrary_module_path")
