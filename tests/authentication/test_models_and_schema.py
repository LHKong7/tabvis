"""Model contracts & Agent-visible schema (design §5.1–§5.4, §16.4)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from tabvis.authentication.models import (
    AgentAuthenticationRequest,
    AuthenticationRequest,
    AuthenticationResult,
    CredentialProfile,
)


def test_agent_request_only_profile_id() -> None:
    assert set(AgentAuthenticationRequest.model_fields) == {"credential_profile_id"}
    req = AgentAuthenticationRequest(credential_profile_id="p1")
    assert req.credential_profile_id == "p1"


def test_agent_request_forbids_extra_fields() -> None:
    for smuggled in (
        {"password": "x"},
        {"username": "x"},
        {"totp": "x"},
        {"secret_ref": "sec_1"},
        {"browser_session_id": "b1"},
        {"task_id": "t1"},
        {"user_id": "u1"},
        {"origin": "https://x.com"},
        {"cookie": "sid=1"},
    ):
        with pytest.raises(ValidationError):
            AgentAuthenticationRequest(credential_profile_id="p1", **smuggled)


def test_result_is_strict_allowlist() -> None:
    r = AuthenticationResult(success=True, authenticated_origin="https://x.com")
    assert r.model_dump() == {
        "success": True,
        "authenticated_origin": "https://x.com",
        "requires_human_interaction": False,
        "error_code": None,
    }
    with pytest.raises(ValidationError):
        AuthenticationResult(success=False, detail="site said no")  # no free-text field allowed


def test_profile_canonicalizes_origins() -> None:
    p = CredentialProfile(
        id="p1",
        owner_user_id="u1",
        allowed_origins=["https://Accounts.Example.com:443/login"],
        allowed_frame_origins=["https://login.example.com."],
        authentication_adapter="generic_password_v1",
    )
    assert p.allowed_origins == ["https://accounts.example.com"]
    assert p.allowed_frame_origins == ["https://login.example.com"]


def test_profile_rejects_non_canonical_origin() -> None:
    for bad in ("http://ex.com", "https://user:pw@ex.com", "ex.com", "*.ex.com"):
        with pytest.raises(ValidationError):
            CredentialProfile(
                id="p1",
                owner_user_id="u1",
                allowed_origins=[bad],
                authentication_adapter="generic_password_v1",
            )


def test_profile_has_no_plaintext_field() -> None:
    # Only *_secret_ref reference fields exist — never a plaintext credential field (§5.4).
    fields = set(CredentialProfile.model_fields)
    assert {"username_secret_ref", "password_secret_ref", "totp_secret_ref"} <= fields
    assert "password" not in fields and "username" not in fields and "totp_seed" not in fields


def test_internal_request_shape() -> None:
    req = AuthenticationRequest(
        request_id="r1",
        browser_session_id="b1",
        credential_profile_id="p1",
        task_id="t1",
        user_id="u1",
        agent_id="a1",
        requested_at=datetime.now(timezone.utc),
    )
    assert req.credential_profile_id == "p1"
