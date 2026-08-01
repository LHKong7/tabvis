"""Secure credential injection & automatic authentication (docs/CREDENTIAL_INJECTION_DESIGN.md).

The package contains the security contracts plus the Phase 6 managed runtime integration: strict
models, stable errors, non-serializable secrets, policy/capabilities, Broker clients, Playwright
Browser Host control, Session Vault, and DLP boundaries. Managed authentication remains feature-gated
and production configuration fails closed unless an external L2 Broker deployment is verified.
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
