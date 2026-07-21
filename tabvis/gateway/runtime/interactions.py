"""The Interaction aggregate — a question or approval a Run is blocked on (design §5.2, §13.3).

Two kinds, deliberately distinct (design §13.3):

* **question** — obtain missing preference/information; resolved by a structured answer. This is what
  the model's ``AskUserQuestion`` tool maps to.
* **approval** — authorize a proposed sensitive action; resolved by allow/deny (with an optional
  scoped grant). This is what a permission ``behavior=ask`` maps to.

They share transport and this record shape but not semantics: a denied approval fails the Run, whereas
a question is never a deny — it only ever carries an answer. An Interaction is short-lived and has at
most one response (§16.2 invariant), enforced by the compare-and-set in the service.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Final

from tabvis.gateway.runtime import runs

# kinds (design §13.3)
KIND_QUESTION: Final = "question"
KIND_APPROVAL: Final = "approval"

# statuses
PENDING: Final = "pending"
ANSWERED: Final = "answered"
EXPIRED: Final = "expired"
CANCELLED: Final = "cancelled"

TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({ANSWERED, EXPIRED, CANCELLED})

# which Run waiting-state each kind puts the Run into (design §5.2, §7.4).
WAITING_STATE_FOR_KIND: Final[dict[str, str]] = {
    KIND_QUESTION: runs.WAITING_FOR_INPUT,
    KIND_APPROVAL: runs.WAITING_FOR_APPROVAL,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class InteractionRecord:
    """One pending question/approval (design §5.2, §12.2 ``interactions``).

    ``request`` is the channel-agnostic payload the UI renders (question text + options/schema, or the
    proposed action for an approval). ``answer`` is the structured response once given. Neither field
    may carry secrets — an Interaction crosses to untrusted channels (design principle: secrets are
    references).
    """

    interaction_id: str
    run_id: str
    kind: str
    agent_id: str | None = None
    session_id: str | None = None
    status: str = PENDING
    request: dict[str, Any] = field(default_factory=dict)
    answer: dict[str, Any] | None = None
    response_command_id: str | None = None
    created_at: str = field(default_factory=_utc_now)
    expires_at: str | None = None
    answered_at: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == PENDING

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InteractionRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class InteractionReceipt:
    """The result of responding (design §9.4 "interaction receipt").

    ``duplicate`` marks a receipt replayed for a repeated response command — the answer was not applied
    again (design §5.5, and the Phase 2 "duplicate answers return the original receipt" acceptance).
    """

    interaction_id: str
    status: str
    answered_at: str | None = None
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
