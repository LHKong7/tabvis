"""Approval service (§8.5) and composed authorization (§8.4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tabvis.authentication.approval import ApprovalService
from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext, CredentialProfile
from tabvis.authentication.policy_engine import check_authorization


# --------------------------------------------------------------------------- approval (§8.5)


def test_never_policy_needs_no_approval() -> None:
    svc = ApprovalService()
    assert not svc.requires_approval(
        policy="never", user_id="u1", credential_profile_id="p1", origin="https://x.com"
    )


def test_always_policy_always_asks() -> None:
    svc = ApprovalService()
    svc.record_approval(
        user_id="u1", credential_profile_id="p1", origin="https://x.com", approved_by="u1"
    )
    assert svc.requires_approval(
        policy="always", user_id="u1", credential_profile_id="p1", origin="https://x.com"
    )


def test_first_use_asks_once_then_remembers_exact_origin() -> None:
    svc = ApprovalService()
    args = dict(user_id="u1", credential_profile_id="p1", origin="https://x.com")
    assert svc.requires_approval(policy="first_use", **args)
    svc.record_approval(approved_by="u1", **args)
    assert not svc.requires_approval(policy="first_use", **args)
    # a different origin is NOT covered by the prior approval (§8.5 binds to exact origin)
    assert svc.requires_approval(
        policy="first_use", user_id="u1", credential_profile_id="p1", origin="https://y.com"
    )
    # a different user is not covered either
    assert svc.requires_approval(
        policy="first_use", user_id="u2", credential_profile_id="p1", origin="https://x.com"
    )


def test_revoke_forces_reapproval() -> None:
    svc = ApprovalService()
    args = dict(user_id="u1", credential_profile_id="p1", origin="https://x.com")
    svc.record_approval(approved_by="u1", **args)
    svc.revoke(**args)
    assert svc.requires_approval(policy="first_use", **args)


# --------------------------------------------------------------------------- authorization (§8.4)


def _profile(**overrides) -> CredentialProfile:
    base = dict(
        id="p1",
        owner_user_id="u1",
        allowed_origins=["https://accounts.example.com"],
        allowed_frame_origins=["https://accounts.example.com"],
        authentication_adapter="generic_password_v1",
    )
    base.update(overrides)
    return CredentialProfile(**base)


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
        navigation_generation=1,
        page_id="page-1",
    )
    base.update(overrides)
    return BrowserAuthenticationContext(**base)


def test_happy_path_authorizes() -> None:
    assert check_authorization(profile=_profile(), context=_ctx(), requesting_user_id="u1") is None


def test_disabled_profile() -> None:
    assert (
        check_authorization(profile=_profile(enabled=False), context=_ctx(), requesting_user_id="u1")
        is AuthErrorCode.PROFILE_DISABLED
    )


def test_expired_profile() -> None:
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    assert (
        check_authorization(profile=_profile(expires_at=past), context=_ctx(), requesting_user_id="u1")
        is AuthErrorCode.PROFILE_EXPIRED
    )


def test_wrong_owner_looks_like_not_found() -> None:
    assert (
        check_authorization(profile=_profile(), context=_ctx(), requesting_user_id="intruder")
        is AuthErrorCode.PROFILE_NOT_FOUND
    )


def test_non_https() -> None:
    assert (
        check_authorization(profile=_profile(), context=_ctx(is_https=False), requesting_user_id="u1")
        is AuthErrorCode.HTTPS_REQUIRED
    )
    assert (
        check_authorization(
            profile=_profile(), context=_ctx(certificate_valid=False), requesting_user_id="u1"
        )
        is AuthErrorCode.HTTPS_REQUIRED
    )


def test_origin_mismatch() -> None:
    assert (
        check_authorization(
            profile=_profile(), context=_ctx(top_level_origin="https://evil.test"), requesting_user_id="u1"
        )
        is AuthErrorCode.ORIGIN_NOT_ALLOWED
    )


def test_frame_origin_mismatch() -> None:
    assert (
        check_authorization(
            profile=_profile(),
            context=_ctx(ancestor_frame_origins=["https://ads.evil.test"]),
            requesting_user_id="u1",
        )
        is AuthErrorCode.FRAME_ORIGIN_NOT_ALLOWED
    )


def test_max_uses_exhausted() -> None:
    assert (
        check_authorization(
            profile=_profile(max_uses=3), context=_ctx(), requesting_user_id="u1", uses_so_far=3
        )
        is AuthErrorCode.PROFILE_EXPIRED
    )


def test_environment_gates() -> None:
    assert (
        check_authorization(
            profile=_profile(), context=_ctx(), requesting_user_id="u1", another_lease_held=True
        )
        is AuthErrorCode.BROWSER_LOCKED
    )
    assert (
        check_authorization(
            profile=_profile(), context=_ctx(), requesting_user_id="u1", secret_provider_healthy=False
        )
        is AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE
    )
    assert (
        check_authorization(
            profile=_profile(), context=_ctx(), requesting_user_id="u1", audit_available=False
        )
        is AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR
    )
