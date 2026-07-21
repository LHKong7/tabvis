"""Browser identity and profile-key resolution (design §10.5, §10.2).

One Agent owns one default :class:`BrowserIdentity` (design §10.5). The **profile key** is the exclusive
resource a lease claims: an *isolated* agent gets a key unique to itself (so two isolated agents never
contend), while a *shared named profile* maps every agent to one key (so exactly one active writer is
possible). Identity metadata is agent-readable; secret material — cookies, tokens, passwords, proxy
credentials — is referenced, never inlined (design §10.5, §11.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Metadata keys that must never be exposed to the agent (design §10.5).
_SECRET_KEYS = frozenset({"cookies", "storage_state", "token", "password", "proxy_credentials"})


@dataclass
class BrowserIdentity:
    identity_id: str
    agent_id: str
    profile_key: str
    profile_ref: str            # a directory reference, not secret content
    isolated: bool
    engine: str = "chromium"
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_metadata(self) -> dict[str, Any]:
        """Agent-readable view: secret material is dropped, only references remain (design §10.5)."""
        return {k: v for k, v in self.metadata.items() if k not in _SECRET_KEYS}


def profile_key_for(agent_id: str, profile: str | None) -> str:
    """The exclusive resource key a lease claims (design §10.5).

    Isolated (``profile is None``) → unique per agent; a shared named profile → one key for all agents.
    """
    if profile:
        return f"profile:{profile}"
    return f"agent:{agent_id}"


def resolve_identity(agent_id: str, profile: str | None, *, engine: str = "chromium") -> BrowserIdentity:
    key = profile_key_for(agent_id, profile)
    isolated = profile is None
    return BrowserIdentity(
        identity_id=f"bid_{key.replace(':', '_')}",
        agent_id=agent_id,
        profile_key=key,
        profile_ref=key,   # a real deployment maps this to a directory under browser-os-data
        isolated=isolated,
        engine=engine,
    )
