"""Authentication data models (design §5).

Every model here is a strict, closed schema. The two hard rules of Phase 0:

* the **Agent-visible** request (:class:`AgentAuthenticationRequest`) carries *only* a profile id —
  no username / password / totp / secret_ref / cookie / origin / session id (design §5.1, §16.4);
* nothing that can hold a plaintext secret is a Pydantic field — resolved secrets live only in
  :class:`~tabvis.authentication.secrets.SecretValue`, and :class:`ResolvedCredentials` (which holds
  them) is a plain dataclass that refuses serialization (design §5.8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tabvis.authentication.policy import OriginError, canonicalize_origin
from tabvis.authentication.secrets import SecretValue


# --------------------------------------------------------------------------- Agent-visible surface


class AgentAuthenticationRequest(BaseModel):
    """The ONLY thing the Agent may send (design §5.1).

    ``extra="forbid"`` is load-bearing: it is what makes an Agent attempt to smuggle a
    ``password`` / ``browser_session_id`` / ``origin`` field a hard validation error rather than a
    silently-ignored extra. The trusted context (task/user/session/origin) is added by the
    Orchestrator, never accepted from the Agent.
    """

    model_config = ConfigDict(extra="forbid")

    credential_profile_id: str = Field(
        description="Opaque id of a stored credential profile to authenticate with."
    )


class AuthenticationResult(BaseModel):
    """The ONLY thing returned to the Agent (design §5.3).

    Strict field allowlist — no exception text, selector, username, cookie, URL query, secret ref or
    site response body. ``extra="forbid"`` prevents a well-meaning caller from tacking a ``message`` or
    ``detail`` field on later.
    """

    model_config = ConfigDict(extra="forbid")

    success: bool
    authenticated_origin: str | None = None
    requires_human_interaction: bool = False
    error_code: str | None = None


# --------------------------------------------------------------------------- internal (trusted) surface


class AuthenticationRequest(BaseModel):
    """The internal request the Orchestrator hands the Broker, enriched with trusted context (design §5.2)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    browser_session_id: str
    credential_profile_id: str
    task_id: str
    user_id: str
    agent_id: str
    requested_at: datetime


class CredentialProfile(BaseModel):
    """Stored credential profile — references only, never plaintext (design §5.4).

    Constraints enforced here:

    * ``allowed_origins`` / ``allowed_frame_origins`` MUST be canonical exact Origins (no path, query,
      fragment or implicit wildcard) — validated on construction so a malformed profile can never be
      stored;
    * the ``*_secret_ref`` fields are opaque references only; there is no field that could hold a
      plaintext secret.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    owner_user_id: str
    enabled: bool = True

    allowed_origins: list[str]
    allowed_frame_origins: list[str] = Field(default_factory=list)

    username_secret_ref: str | None = None
    password_secret_ref: str | None = None
    totp_secret_ref: str | None = None

    authentication_adapter: str
    approval_policy: Literal["never", "first_use", "always"] = "first_use"

    session_ttl_seconds: int = 3600
    reusable_across_tasks: bool = False
    max_uses: int | None = None
    expires_at: datetime | None = None

    @field_validator("allowed_origins", "allowed_frame_origins")
    @classmethod
    def _canonical_origins(cls, value: list[str]) -> list[str]:
        canon: list[str] = []
        for entry in value:
            try:
                canon.append(canonicalize_origin(entry))
            except OriginError as exc:
                raise ValueError(f"not a canonical https origin: {entry!r} ({exc})") from exc
        return canon


class BrowserAuthenticationContext(BaseModel):
    """The browser state the Broker reads back from the Browser Host before authorizing (design §5.5).

    The Broker never *infers* these — ``about:blank`` / ``srcdoc`` effective origins are computed by
    the browser and reported here (design §8.2).
    """

    model_config = ConfigDict(extra="forbid")

    browser_session_id: str
    top_level_url: str
    top_level_origin: str
    frame_url: str
    frame_origin: str
    ancestor_frame_origins: list[str] = Field(default_factory=list)
    is_https: bool
    certificate_valid: bool
    navigation_generation: int
    page_id: str


class CredentialCapability(BaseModel):
    """A one-time, short-lived, fully-bound authorization to perform exactly one authentication (design §5.6).

    Bound to user + task + browser session + page + navigation generation + origin, single-use, and
    default TTL ≤ 30s. It MUST NOT be returned to the Agent, frontend, ordinary API, logs or audit
    (design §5.6) — it lives only in trusted-domain memory. ``remaining_uses`` is pinned to 1 by the
    type system.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    credential_profile_id: str
    browser_session_id: str
    task_id: str
    user_id: str
    top_level_origin: str
    frame_origin: str
    page_id: str
    navigation_generation: int
    allowed_operation: Literal["authenticate"] = "authenticate"
    issued_at: datetime
    expires_at: datetime
    remaining_uses: Literal[1] = 1


# --------------------------------------------------------------------------- resolved secrets (never a model)


@dataclass
class ResolvedCredentials:
    """Short-lived bundle of resolved secrets — created and destroyed ONLY inside the Executor (design §5.8).

    Deliberately a plain dataclass, not a Pydantic model: it holds :class:`SecretValue`s and MUST NOT
    be serializable, must not cross IPC back to the Broker client, and must not be stored on a task
    object or an exception. Every serialization dunder is closed the same way :class:`SecretValue`
    closes them, so a stray ``json.dumps`` / ``pickle`` / f-string over it raises instead of leaking.
    """

    username: SecretValue | None = None
    password: SecretValue | None = None
    totp_seed: SecretValue | None = None

    def release(self) -> None:
        """Release every held secret buffer. Idempotent; safe to call in ``finally`` / cancel paths."""
        for value in (self.username, self.password, self.totp_seed):
            if value is not None:
                value.release()

    def __str__(self) -> str:  # noqa: D105
        from tabvis.authentication.secrets import SecretLeakError

        raise SecretLeakError("ResolvedCredentials cannot be stringified")

    def __repr__(self) -> str:  # noqa: D105
        return "<ResolvedCredentials redacted>"

    def __reduce__(self):  # noqa: ANN204
        from tabvis.authentication.secrets import SecretLeakError

        raise SecretLeakError("ResolvedCredentials cannot be pickled")
