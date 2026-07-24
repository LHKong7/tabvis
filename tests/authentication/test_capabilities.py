"""One-time capability issue/consume (design §5.6, §7.2, §13, §16.1)."""

from __future__ import annotations

import pytest

from tabvis.authentication.capabilities import CapabilityStore
from tabvis.authentication.errors import AuthenticationError, AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext


def _ctx(**overrides) -> BrowserAuthenticationContext:
    base = dict(
        browser_session_id="b1",
        top_level_url="https://accounts.example.com/login",
        top_level_origin="https://accounts.example.com",
        frame_url="https://accounts.example.com/login",
        frame_origin="https://accounts.example.com",
        ancestor_frame_origins=[],
        is_https=True,
        certificate_valid=True,
        navigation_generation=7,
        page_id="page-1",
    )
    base.update(overrides)
    return BrowserAuthenticationContext(**base)


def _issue(store: CapabilityStore, ctx=None, ttl=30):
    return store.issue(
        credential_profile_id="p1",
        context=ctx or _ctx(),
        task_id="t1",
        user_id="u1",
        ttl_seconds=ttl,
    )


def test_capability_is_bound_and_single_use_1() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    assert cap.remaining_uses == 1
    assert cap.allowed_operation == "authenticate"
    assert cap.navigation_generation == 7 and cap.page_id == "page-1"


def test_consume_once_succeeds() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    consumed = store.consume(cap.id, context=_ctx())
    assert consumed.id == cap.id


def test_second_consume_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    store.consume(cap.id, context=_ctx())
    with pytest.raises(AuthenticationError) as exc:
        store.consume(cap.id, context=_ctx())
    assert exc.value.code is AuthErrorCode.CAPABILITY_CONSUMED


def test_unknown_capability_reports_consumed() -> None:
    store = CapabilityStore()
    with pytest.raises(AuthenticationError) as exc:
        store.consume("cap_does_not_exist", context=_ctx())
    assert exc.value.code is AuthErrorCode.CAPABILITY_CONSUMED


def test_expired_capability_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store, ttl=-1)  # already expired
    with pytest.raises(AuthenticationError) as exc:
        store.consume(cap.id, context=_ctx())
    assert exc.value.code is AuthErrorCode.CAPABILITY_EXPIRED


def test_navigation_generation_change_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    with pytest.raises(AuthenticationError) as exc:
        store.consume(cap.id, context=_ctx(navigation_generation=8))
    assert exc.value.code is AuthErrorCode.PAGE_CHANGED


def test_page_id_change_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    with pytest.raises(AuthenticationError) as exc:
        store.consume(cap.id, context=_ctx(page_id="page-2"))
    assert exc.value.code is AuthErrorCode.PAGE_CHANGED


def test_origin_change_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    with pytest.raises(AuthenticationError) as exc:
        store.consume(cap.id, context=_ctx(frame_origin="https://evil.test"))
    assert exc.value.code is AuthErrorCode.ORIGIN_NOT_ALLOWED


def test_invalidate_then_consume_fails() -> None:
    store = CapabilityStore()
    cap = _issue(store)
    store.invalidate(cap.id)
    with pytest.raises(AuthenticationError):
        store.consume(cap.id, context=_ctx())


def test_purge_expired() -> None:
    store = CapabilityStore()
    _issue(store, ttl=-1)
    _issue(store, ttl=-1)
    live = _issue(store, ttl=30)
    assert store.purge_expired() == 2
    # the live one still consumes
    assert store.consume(live.id, context=_ctx()).id == live.id
