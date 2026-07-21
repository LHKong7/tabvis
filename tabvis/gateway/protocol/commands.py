"""The Command envelope and command vocabulary (design §5.5, §9.2).

**Commands change state.** A command is a request to mutate; a handler validates it, applies the
mutation transactionally, and emits the resulting event(s). Two rules from the design shape this
module:

* Every command carries a globally unique ``command_id`` and handlers MUST be **idempotent** for it:
  a duplicate returns the original :class:`CommandResult` rather than mutating twice (design §5.5,
  §3.1 Command Router). The idempotency ledger lives in the store; this module is the schema.
* Body fields never override identity established by credentials (design §3.1) — the Principal is
  attached by the access layer, not read from ``data``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tabvis.gateway import PROTOCOL
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.errors import GatewayError


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommandType:
    """The command vocabulary (design §9.4 core methods)."""

    CONVERSATION_CREATE = "conversation.create"
    RUN_CREATE = "run.create"
    RUN_CANCEL = "run.cancel"
    INTERACTION_RESPOND = "interaction.respond"
    SESSION_FORK = "session.fork"
    SESSION_COMPACT = "session.compact"
    AGENT_QUIT = "agent.quit"


@dataclass
class Command:
    """One command envelope (design §9.2).

    ``command_id`` defaults to a freshly minted id, but a client SHOULD supply its own so a retried
    request is recognised as the same logical command.
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=ids.new_command_id)
    issued_at: str = field(default_factory=_utc_now)
    protocol: str = PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "command_id": self.command_id,
            "type": self.type,
            "issued_at": self.issued_at,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Command":
        """Parse and validate a wire command body, raising :class:`GatewayError` on a bad envelope."""
        if not isinstance(payload, dict):
            raise GatewayError("VALIDATION_FAILED", message="Command body must be an object")
        protocol = payload.get("protocol", PROTOCOL)
        if protocol != PROTOCOL:
            raise GatewayError(
                "UNSUPPORTED_PROTOCOL",
                message=f"Expected protocol {PROTOCOL!r}, got {protocol!r}",
                details={"expected": PROTOCOL, "received": protocol},
            )
        ctype = payload.get("type")
        if not isinstance(ctype, str) or not ctype:
            raise GatewayError("VALIDATION_FAILED", message="Command 'type' is required")
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise GatewayError("VALIDATION_FAILED", message="Command 'data' must be an object")
        command_id = payload.get("command_id") or ids.new_command_id()
        return cls(
            type=ctype,
            data=data,
            command_id=command_id,
            issued_at=payload.get("issued_at") or _utc_now(),
            protocol=protocol,
        )


@dataclass
class CommandResult:
    """The outcome of handling a command.

    ``duplicate`` marks a result replayed from the idempotency ledger (design §5.5): the mutation was
    not applied again, the original outcome is returned verbatim.
    """

    command_id: str
    status: str = "accepted"  # accepted | rejected
    data: dict[str, Any] = field(default_factory=dict)
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status,
            "duplicate": self.duplicate,
            "data": self.data,
        }
