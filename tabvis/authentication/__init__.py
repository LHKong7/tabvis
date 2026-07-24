"""Secure credential injection & automatic authentication (docs/CREDENTIAL_INJECTION_DESIGN.md).

Phase 0 (security contract & test skeleton): the data models, stable error codes, the non-serializable
:class:`~tabvis.authentication.secrets.SecretValue`, origin/frame policy primitives, one-time
capabilities, the reference-only profile store and whitelist audit events. No automatic login runs yet
(that is Phase 1+); the guarantee of this phase is that **no new interface can accept or emit a
plaintext secret** (design §15 Phase 0 acceptance).
"""

from __future__ import annotations

from tabvis.authentication.errors import (
    AuthenticationError,
    AuthErrorCode,
    is_retryable,
    requires_re_request,
)
from tabvis.authentication.models import (
    AgentAuthenticationRequest,
    AuthenticationRequest,
    AuthenticationResult,
    BrowserAuthenticationContext,
    CredentialCapability,
    CredentialProfile,
    ResolvedCredentials,
)
from tabvis.authentication.approval import ApprovalRecord, ApprovalService
from tabvis.authentication.capabilities import CapabilityStore
from tabvis.authentication.policy_engine import check_authorization
from tabvis.authentication.secrets import (
    BufferSecretValue,
    SecretLeakError,
    SecretValue,
    secret_from_str,
)
from tabvis.authentication.totp import generate_totp, totp_candidates

__all__ = [
    "AgentAuthenticationRequest",
    "ApprovalRecord",
    "ApprovalService",
    "AuthErrorCode",
    "AuthenticationError",
    "AuthenticationRequest",
    "AuthenticationResult",
    "BrowserAuthenticationContext",
    "BufferSecretValue",
    "CapabilityStore",
    "CredentialCapability",
    "CredentialProfile",
    "ResolvedCredentials",
    "SecretLeakError",
    "SecretValue",
    "check_authorization",
    "generate_totp",
    "is_retryable",
    "requires_re_request",
    "secret_from_str",
    "totp_candidates",
]
