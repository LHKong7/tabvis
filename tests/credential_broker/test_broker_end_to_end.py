"""In-process Broker end-to-end (design §7.1) — the full flow with fakes (L0)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import AuthenticationRequest
from tabvis.credential_broker.broker import CredentialBroker, new_request_id
from tabvis.credential_broker.secrets.memory import MemorySecretProvider
from tabvis.dlp import canary


@pytest.fixture(autouse=True)
def _clean_canary():
    canary.clear()
    yield
    canary.clear()


@pytest.fixture(autouse=True)
def _bind(browser_cls, make_context, make_profile):
    # expose the conftest fakes as module globals so the helper functions below can use them
    global FakeAuthBrowser, default_context, default_profile
    FakeAuthBrowser, default_context, default_profile = browser_cls, make_context, make_profile
    yield


def _request(**overrides) -> AuthenticationRequest:
    base = dict(
        request_id=new_request_id(),
        browser_session_id="b1",
        credential_profile_id="p1",
        task_id="t1",
        user_id="u1",
        agent_id="a1",
        requested_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return AuthenticationRequest(**base)


def _make_broker(browser, *, profile=None, provider=None, audit=None, approval_cb=None):
    profile = profile or default_profile()
    provider = provider or MemorySecretProvider({"sec_user": "aliceuser", "sec_pass": "hunter2xyz"})

    def lookup(pid, uid):
        return profile if (pid == profile.id and uid == profile.owner_user_id) else None

    return CredentialBroker(
        provider=provider,
        profile_lookup=lookup,
        browser_provider=lambda sid: browser,
        audit_sink=audit,
        approval_callback=approval_cb,
    )


def _run(coro):
    return asyncio.run(coro)


def test_successful_authentication() -> None:
    browser = FakeAuthBrowser()
    events: list[dict] = []
    broker = _make_broker(browser, audit=events.append)
    result = _run(broker.authenticate(_request()))
    assert result.success
    assert result.authenticated_origin == "https://accounts.example.com"
    assert result.error_code is None
    # password + username were typed; a submit happened
    assert {r for r, _ in browser.typed} == {"username", "password"}
    # a whitelist audit event was recorded, success=True, no secret fields
    assert len(events) == 1 and events[0]["success"] is True
    assert "password" not in events[0]


def test_canary_registered_for_resolved_secrets() -> None:
    browser = FakeAuthBrowser()
    broker = _make_broker(browser)
    _run(broker.authenticate(_request()))
    # the resolved password value is now a registered canary (DLP would block it on egress)
    assert canary.is_registered(b"hunter2xyz")
    assert canary.is_registered(b"aliceuser")


def test_wrong_owner_is_profile_not_found() -> None:
    browser = FakeAuthBrowser()
    broker = _make_broker(browser)
    result = _run(broker.authenticate(_request(user_id="intruder")))
    assert not result.success
    assert result.error_code == AuthErrorCode.PROFILE_NOT_FOUND.value


def test_origin_mismatch_denied_before_secrets() -> None:
    browser = FakeAuthBrowser(context=default_context(top_level_origin="https://evil.test"))
    broker = _make_broker(browser)
    result = _run(broker.authenticate(_request()))
    assert result.error_code == AuthErrorCode.ORIGIN_NOT_ALLOWED.value
    # no field was ever typed — denial happened before any secret resolution
    assert browser.typed == []


def test_provider_unavailable_fails_safe() -> None:
    browser = FakeAuthBrowser()
    broker = _make_broker(browser, provider=MemorySecretProvider(healthy=False))
    result = _run(broker.authenticate(_request()))
    assert result.error_code == AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE.value


def test_missing_secret_is_credential_missing() -> None:
    browser = FakeAuthBrowser()
    # profile references a password ref the provider doesn't have
    broker = _make_broker(browser, provider=MemorySecretProvider({"sec_user": "alice"}))
    result = _run(broker.authenticate(_request()))
    assert result.error_code == AuthErrorCode.CREDENTIAL_MISSING.value


def test_first_use_approval_denied() -> None:
    browser = FakeAuthBrowser()

    async def deny(_req, _origin):
        return False

    broker = _make_broker(
        browser, profile=default_profile(approval_policy="first_use"), approval_cb=deny
    )
    result = _run(broker.authenticate(_request()))
    assert result.error_code == AuthErrorCode.APPROVAL_DENIED.value
    assert browser.typed == []  # denied before any secret use


def test_first_use_approval_granted_then_remembered() -> None:
    browser = FakeAuthBrowser()
    calls = {"n": 0}

    async def approve(_req, _origin):
        calls["n"] += 1
        return True

    broker = _make_broker(
        browser, profile=default_profile(approval_policy="first_use"), approval_cb=approve
    )
    assert _run(broker.authenticate(_request())).success
    assert _run(broker.authenticate(_request())).success
    # approval prompted only on first use for this (user, profile, origin)
    assert calls["n"] == 1


def test_rejected_login_clears_fields() -> None:
    browser = FakeAuthBrowser(succeeds=False)
    broker = _make_broker(browser)
    result = _run(broker.authenticate(_request()))
    assert not result.success
    assert result.error_code == AuthErrorCode.AUTHENTICATION_REJECTED.value
    assert browser.cleared >= 1


def test_max_uses_enforced() -> None:
    browser = FakeAuthBrowser()
    broker = _make_broker(browser, profile=default_profile(max_uses=1))
    assert _run(broker.authenticate(_request())).success
    second = _run(broker.authenticate(_request()))
    assert second.error_code == AuthErrorCode.PROFILE_EXPIRED.value
