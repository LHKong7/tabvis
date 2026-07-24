"""Stable authentication error codes (design §12.2).

The Agent only ever receives an ``error_code`` from this closed set — never an exception message,
selector, username, URL query or provider stack trace (design §5.3, §12.1). The UI maps a code to a
human explanation from a local catalog; the code itself is the whole wire contract.
"""

from __future__ import annotations

from enum import Enum


class AuthErrorCode(str, Enum):
    """The complete, stable set of authentication error codes returned to the Agent."""

    PROFILE_NOT_FOUND = "profile_not_found"
    PROFILE_DISABLED = "profile_disabled"
    PROFILE_EXPIRED = "profile_expired"
    APPROVAL_DENIED = "approval_denied"
    APPROVAL_EXPIRED = "approval_expired"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    FRAME_ORIGIN_NOT_ALLOWED = "frame_origin_not_allowed"
    HTTPS_REQUIRED = "https_required"
    PAGE_CHANGED = "page_changed"
    BROWSER_LOCKED = "browser_locked"
    CAPABILITY_EXPIRED = "capability_expired"
    CAPABILITY_CONSUMED = "capability_consumed"
    SECRET_PROVIDER_UNAVAILABLE = "secret_provider_unavailable"
    CREDENTIAL_MISSING = "credential_missing"
    AUTHENTICATION_REJECTED = "authentication_rejected"
    AUTHENTICATION_TIMEOUT = "authentication_timeout"
    HUMAN_INTERACTION_REQUIRED = "human_interaction_required"
    DLP_BLOCKED = "dlp_blocked"
    INTERNAL_AUTHENTICATION_ERROR = "internal_authentication_error"


# Retry classification (design §12.2). Values mirror the "可重试" column: retryable codes MAY be
# retried by the caller; ``re_request`` codes require a fresh authorization (new Capability / approval)
# rather than a bare retry; ``no`` codes are terminal.
_RETRYABLE: frozenset[AuthErrorCode] = frozenset(
    {
        AuthErrorCode.BROWSER_LOCKED,
        AuthErrorCode.SECRET_PROVIDER_UNAVAILABLE,
        AuthErrorCode.AUTHENTICATION_TIMEOUT,
        AuthErrorCode.INTERNAL_AUTHENTICATION_ERROR,
    }
)

_RE_REQUEST: frozenset[AuthErrorCode] = frozenset(
    {
        AuthErrorCode.APPROVAL_EXPIRED,
        AuthErrorCode.PAGE_CHANGED,
        AuthErrorCode.CAPABILITY_EXPIRED,
    }
)


def is_retryable(code: AuthErrorCode) -> bool:
    """Whether a bare retry of the same request may succeed (design §12.2 "可重试")."""
    return code in _RETRYABLE


def requires_re_request(code: AuthErrorCode) -> bool:
    """Whether the caller must obtain a fresh authorization rather than retry as-is."""
    return code in _RE_REQUEST


class AuthenticationError(Exception):
    """Internal, trusted-domain authentication failure carrying a stable :class:`AuthErrorCode`.

    This exception stays inside the trusted domain (Broker / Executor). It is translated to a bare
    :class:`~tabvis.authentication.models.AuthenticationResult` (``error_code`` only) before anything
    crosses back to the Orchestrator or the Agent — its ``args`` MUST NOT reach the model, logs or
    telemetry (design §12.1). Keep the message free of secrets, selectors and site text.
    """

    def __init__(self, code: AuthErrorCode, *, detail: str | None = None) -> None:
        self.code = code
        # ``detail`` is for trusted-domain debugging only and is dropped at the DLP/result boundary.
        self.detail = detail
        super().__init__(code.value)
