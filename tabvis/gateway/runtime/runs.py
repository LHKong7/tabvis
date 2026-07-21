"""The immutable Run aggregate and its state machine (design §7.3, §7.4).

A Run is one prompt-to-terminal execution attempt. Unlike today's reused ``AgentRecord`` (whose
prompt/counters/outcome are overwritten on continuation), a Run is created once and only its
*status* and progress advance — the record of what happened is never lost.

The state machine is the design's §7.4 diagram, encoded as an explicit adjacency table. Two
invariants from §16.2 are enforced here and relied on by the store's compare-and-set:

* terminal states never transition;
* only a declared edge is allowed — everything else raises ``INVALID_STATE_TRANSITION``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Final

from tabvis.gateway.protocol.errors import GatewayError

# --- states (design §7.3 RunRecord.status literal) ---------------------------------------------

QUEUED: Final = "queued"
PREPARING: Final = "preparing"
RUNNING: Final = "running"
WAITING_FOR_INPUT: Final = "waiting_for_input"
WAITING_FOR_APPROVAL: Final = "waiting_for_approval"
RETRYING: Final = "retrying"
CANCELLING: Final = "cancelling"
COMPLETED: Final = "completed"
FAILED: Final = "failed"
CANCELLED: Final = "cancelled"
INTERRUPTED: Final = "interrupted"

TERMINAL: Final[frozenset[str]] = frozenset({COMPLETED, FAILED, CANCELLED, INTERRUPTED})
WAITING: Final[frozenset[str]] = frozenset({WAITING_FOR_INPUT, WAITING_FOR_APPROVAL})
ACTIVE: Final[frozenset[str]] = frozenset(
    {QUEUED, PREPARING, RUNNING, WAITING_FOR_INPUT, WAITING_FOR_APPROVAL, RETRYING, CANCELLING}
)

# The design's §7.4 edges, source → allowed destinations. Terminal states have no outgoing edges.
_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    QUEUED: frozenset({PREPARING, CANCELLING, CANCELLED}),
    PREPARING: frozenset({RUNNING, FAILED, CANCELLING, CANCELLED}),
    RUNNING: frozenset(
        {
            WAITING_FOR_INPUT,
            WAITING_FOR_APPROVAL,
            RETRYING,
            CANCELLING,
            COMPLETED,
            FAILED,
            INTERRUPTED,
        }
    ),
    WAITING_FOR_INPUT: frozenset({RUNNING, CANCELLING, CANCELLED}),
    WAITING_FOR_APPROVAL: frozenset({RUNNING, FAILED, CANCELLING, CANCELLED}),
    RETRYING: frozenset({RUNNING, FAILED}),
    CANCELLING: frozenset({CANCELLED}),
    COMPLETED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
    INTERRUPTED: frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    """True iff ``src → dst`` is a declared edge (design §7.4)."""
    return dst in _TRANSITIONS.get(src, frozenset())


def assert_transition(src: str, dst: str) -> None:
    """Raise ``INVALID_STATE_TRANSITION`` unless ``src → dst`` is allowed."""
    if not can_transition(src, dst):
        raise GatewayError(
            "INVALID_STATE_TRANSITION",
            message=f"Run cannot transition {src!r} → {dst!r}",
            details={"from": src, "to": dst},
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    """One execution attempt (design §7.3).

    Created once and advanced only through :func:`assert_transition`-checked status changes. Timing
    and progress counters are per-Run and never reset — the whole point of splitting Run out of Agent.
    """

    run_id: str
    agent_id: str
    session_id: str
    command_id: str
    prompt_message_id: str = ""
    conversation_id: str | None = None
    workspace_id: str | None = None
    attempt: int = 1
    status: str = QUEUED
    model: str = ""
    max_turns: int | None = None
    turns: int = 0
    tool_calls: int = 0
    result_message_id: str | None = None
    error_code: str | None = None
    created_at: str = field(default_factory=_utc_now)
    started_at: str | None = None
    ended_at: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE

    @property
    def is_waiting(self) -> bool:
        return self.status in WAITING

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
