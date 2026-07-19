"""Browser identity — the ``BrowserIdentity`` record and ``IdentityBinding`` vocabulary (IDP-1).

This is the *data-only* seam for the Identity Context in ``design.md`` (§1). It defines the record
shape the design calls for — a durable, ``agent_id``-keyed ``BrowserIdentity`` with profile / auth /
network / environment / permissions sub-objects, plus a transient ``IdentityBinding`` — WITHOUT any
wiring. Nothing resolves, persists, or enforces these yet: today an agent's identity is still just
its persistent Chromium profile directory guarded by ``manager.owner_agent`` (see ``design.md`` §1
"当前实现"). Materializing the record at spawn and making it the profile source is IDP-2/IDP-3;
the binding lifecycle over the owner/busy lock is IDP-4.

Fields mirror ``design.md``'s Identity data model, in snake_case. Anything that would hold a secret
is a ``*_ref`` (an opaque reference into a future secret store), never the secret itself — the
design's core invariant that "Identity Metadata 可以被 Agent 读取，但密码、Token、完整 Cookie 和
Proxy 密钥不能直接暴露给 Agent".
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from tabvis.browser.session import utc_now

# The identity's lifecycle state (``design.md`` §1 data model). Only the shape is defined here; the
# state-machine that transitions between them is IDP-4.
IdentityStatus = Literal["ready", "in_use", "expired", "disabled"]
IDENTITY_STATUSES: tuple[IdentityStatus, ...] = ("ready", "in_use", "expired", "disabled")


def new_identity_id() -> str:
    return f"id_{uuid.uuid4().hex[:16]}"


def new_binding_id() -> str:
    return f"bnd_{uuid.uuid4().hex[:16]}"


@dataclass
class IdentityProfile:
    """Chromium User Data + browser preferences (``design.md`` identity.profile)."""

    profile_ref: str | None = None      # persistent-profile reference (today: the user_data_dir)
    browser_version: str | None = None
    preferences: dict[str, Any] = field(default_factory=dict)


@dataclass
class IdentityAuth:
    """Cookie / storage / credential references (``design.md`` identity.auth). Refs only, no secrets."""

    storage_state_ref: str | None = None   # encrypted cookie+storage blob reference
    credential_refs: list[str] = field(default_factory=list)  # password/token secret references
    expires_at: str | None = None


@dataclass
class IdentityNetwork:
    """Proxy / region / policy (``design.md`` identity.network). ``proxy_ref``, never the URL."""

    proxy_ref: str | None = None
    region: str | None = None
    policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class IdentityEnvironment:
    """User-agent / locale / timezone / viewport / platform (``design.md`` identity.environment)."""

    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    viewport: dict[str, int] | None = None
    platform: str | None = None


@dataclass
class IdentityPermissions:
    """Origin allow/deny + capabilities + confirmation rules (``design.md`` identity.permissions)."""

    allowed_origins: list[str] = field(default_factory=list)
    denied_origins: list[str] = field(default_factory=list)
    browser_capabilities: list[str] = field(default_factory=list)
    confirmation_rules: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BrowserIdentity:
    """An agent's long-lived browser identity — the design's ``BrowserIdentity`` record.

    ``agent_id`` is the permanent owner (a UNIQUE key in the design's persistence layer); an agent
    has exactly one identity and an identity belongs to exactly one agent. Only ``IdentityMetadata``
    (this record minus the underlying secrets the ``*_ref`` fields point at) is ever handed to an
    agent.
    """

    agent_id: str                         # UNIQUE owner
    id: str = field(default_factory=new_identity_id)
    name: str | None = None
    status: IdentityStatus = "ready"
    profile: IdentityProfile = field(default_factory=IdentityProfile)
    auth: IdentityAuth = field(default_factory=IdentityAuth)
    network: IdentityNetwork = field(default_factory=IdentityNetwork)
    environment: IdentityEnvironment = field(default_factory=IdentityEnvironment)
    permissions: IdentityPermissions = field(default_factory=IdentityPermissions)
    created_at: str = field(default_factory=utc_now)
    last_used_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def metadata(self) -> dict[str, Any]:
        """``IdentityMetadata`` — the agent-readable view. Same shape today (fields are refs, not

        secrets); a real secret store (IDP-6) would additionally strip anything sensitive here.
        """
        return self.to_dict()


@dataclass
class IdentityBinding:
    """A transient acquisition of an identity for one Runtime Session (``design.md`` Runtime Binding).

    Valid only for the current session; Execution acts through ``binding_id`` and never reaches the
    underlying profile/credentials directly. Minting/retiring this over the existing owner/busy lock
    is IDP-4 — here it is just the shape.
    """

    identity_id: str
    agent_id: str
    workspace_id: str | None = None
    binding_id: str = field(default_factory=new_binding_id)
    browser_context_ref: str | None = None
    capabilities: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
