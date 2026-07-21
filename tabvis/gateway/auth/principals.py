"""The Principal — who a command acts as (design §3.1, §13.2).

A Principal is attached to every command by the access layer, resolved from credentials — never read
from a request body (design §3.1). Authorization is always against a Principal and a Resource
(design principle), so ownership isolation (an agent reaching only its own runs) precedes any policy
fallback (design §13.2).

For a ``kind == "agent"`` principal the ``principal_id`` **is** the agent id — the principal is that
agent — which is what :meth:`Principal.can_access_agent` keys on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Principal:
    principal_id: str
    kind: Literal["user", "service", "agent", "channel"]
    tenant_id: str = "local"
    roles: tuple[str, ...] = ()
    channel_id: str | None = None
    external_account_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    def can_access_agent(self, agent_id: str | None) -> bool:
        """True iff this principal may act on ``agent_id``'s resources (design §13.2 isolation)."""
        if self.is_admin:
            return True
        if self.kind == "agent":
            return agent_id is not None and agent_id == self.principal_id
        return False


def local_admin() -> Principal:
    """The loopback/dev principal — full access, matching today's open localhost console (§3.1)."""
    return Principal(principal_id="local-admin", kind="user", tenant_id="local", roles=("admin",))


def agent_principal(agent_id: str) -> Principal:
    """A principal scoped to exactly one agent (the per-agent credential path, §3.1)."""
    return Principal(principal_id=agent_id, kind="agent", tenant_id="local", roles=("agent",))
