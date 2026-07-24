"""Composed authorization check (design §8.4).

:func:`check_authorization` runs the deterministic pre-conditions the Broker evaluates before issuing a
capability (design §7.1 step 7). It returns ``None`` when authorization may proceed, or the stable
:class:`~tabvis.authentication.errors.AuthErrorCode` for the first failing gate — the Broker turns that
into a redacted result.

Only the checks that are pure functions of (profile, context, counters) live here. The stateful
environment gates the design also lists in §8.4 — no other lease held, run not cancelled, Secret
Provider healthy, audit sink available — are supplied by the Broker as simple booleans so this stays
unit-testable, but they are enforced here so there is one authorization decision, not many.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tabvis.authentication.errors import AuthErrorCode
from tabvis.authentication.models import BrowserAuthenticationContext, CredentialProfile
from tabvis.authentication.policy import frame_chain_authorized, origin_matches


def _now() -> datetime:
    return datetime.now(timezone.utc)


def check_authorization(
    *,
    profile: CredentialProfile,
    context: BrowserAuthenticationContext,
    requesting_user_id: str,
    uses_so_far: int = 0,
    another_lease_held: bool = False,
    run_cancelled: bool = False,
    secret_provider_healthy: bool = True,
    audit_available: bool = True,
) -> AuthErrorCode | None:
    """Return the first failing gate's error code, or ``None`` if authorization may proceed (§8.4).

    Order matters: cheaper / more-specific denials come first so the returned code is the most
    actionable one. Every branch maps to a stable code from §12.2.
    """
    # -- profile state -------------------------------------------------------------------------
    if not profile.enabled:
        return AuthErrorCode.PROFILE_DISABLED
    if profile.expires_at is not None and _now() >= _as_aware(profile.expires_at):
        return AuthErrorCode.PROFILE_EXPIRED

    # -- ownership (design §5.4: every use re-checks owner) ------------------------------------
    if profile.owner_user_id != requesting_user_id:
        # Same redaction as profile_store.get_for_user: don't reveal it exists for another user.
        return AuthErrorCode.PROFILE_NOT_FOUND

    # -- transport / origin --------------------------------------------------------------------
    if not (context.is_https and context.certificate_valid):
        return AuthErrorCode.HTTPS_REQUIRED
    if not origin_matches(context.top_level_origin, profile.allowed_origins):
        return AuthErrorCode.ORIGIN_NOT_ALLOWED
    if not frame_chain_authorized(
        context.frame_origin, context.ancestor_frame_origins, profile.allowed_frame_origins
    ):
        return AuthErrorCode.FRAME_ORIGIN_NOT_ALLOWED

    # -- usage counters ------------------------------------------------------------------------
    if profile.max_uses is not None and uses_so_far >= profile.max_uses:
        return AuthErrorCode.PROFILE_EXPIRED

    # -- environment gates ---------------------------------------------------------------------
    if another_lease_held:
        return AuthErrorCode.BROWSER_LOCKED
    if run_cancelled:
        return AuthErrorCode.AUTHENTICATION_TIMEOUT
    if not secret_provider_healthy:
        return AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE
    if not audit_available:
        # Fail closed when the audit sink is down (design §8.4, §17 TABVIS_AUTH_AUDIT_FAIL_CLOSED).
        return AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR

    return None


def _as_aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC so comparisons never raise on tz-mismatch."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
