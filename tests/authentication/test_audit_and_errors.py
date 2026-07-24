"""Whitelist audit events and stable error codes (design §12.1, §12.2, §16.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tabvis.authentication.audit import build_credential_used_event
from tabvis.authentication.errors import (
    AuthenticationError,
    AuthErrorCode,
    is_retryable,
    requires_re_request,
)


def test_audit_event_whitelist() -> None:
    ev = build_credential_used_event(
        request_id="authreq_123",
        credential_profile_id="production_account",
        origin="https://accounts.example.com",
        task_id="task_123",
        user_id="user_456",
        approved_by="user_456",
        adapter="example_sso_v1",
        success=True,
    )
    dumped = ev.model_dump()
    assert set(dumped) == {
        "event",
        "request_id",
        "credential_profile_id",
        "origin",
        "task_id",
        "user_id",
        "approved_by",
        "adapter",
        "success",
        "error_code",
        "timestamp",
    }
    # forbidden fields have no path onto the record
    for forbidden in ("password", "username", "cookie", "secret_ref", "dom", "url_query"):
        assert forbidden not in dumped
    assert dumped["timestamp"].endswith("Z")


def test_audit_event_forbids_extra() -> None:
    from tabvis.authentication.audit import CredentialAuditEvent

    with pytest.raises(ValidationError):
        CredentialAuditEvent(
            event="e",
            request_id="r",
            credential_profile_id="p",
            origin=None,
            task_id="t",
            user_id="u",
            approved_by=None,
            adapter="a",
            success=False,
            error_code=None,
            timestamp="2026-07-24T10:00:00Z",
            password="leak",  # extra → reject
        )


def test_all_error_codes_are_lowercase_snake() -> None:
    for code in AuthErrorCode:
        assert code.value == code.value.lower()
        assert " " not in code.value


def test_retry_classification() -> None:
    assert is_retryable(AuthErrorCode.BROWSER_LOCKED)
    assert is_retryable(AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE)
    assert not is_retryable(AuthErrorCode.CAPABILITY_CONSUMED)
    assert requires_re_request(AuthErrorCode.PAGE_CHANGED)
    assert requires_re_request(AuthErrorCode.CAPABILITY_EXPIRED)
    assert not requires_re_request(AuthErrorCode.ORIGIN_NOT_ALLOWED)


def test_authentication_error_message_is_just_the_code() -> None:
    err = AuthenticationError(AuthErrorCode.ORIGIN_NOT_ALLOWED, detail="internal only")
    assert str(err) == "origin_not_allowed"
    assert err.code is AuthErrorCode.ORIGIN_NOT_ALLOWED
