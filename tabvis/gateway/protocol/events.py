"""The append-only Event envelope and event catalog (design §5.3, §9.3, §14.2).

**Commands change state; events report facts.** Every persisted lifecycle transition emits exactly
one event (design §19 rule 6). An event is immutable and carries two orderings:

* ``cursor`` — a globally monotonic position across the whole log, used for resumable subscriptions
  (a client reconnects with its last cursor and the server replays everything after it).
* ``seq``    — a monotonic sequence **within one aggregate** (this run, this session), so a consumer
  can detect a gap or an out-of-order duplicate for a single entity (design §5.5).

This module is pure schema. The durable append that *assigns* cursor/seq lives in
:mod:`tabvis.gateway.events.store`; the ``EventType`` constants below are the catalog that store and
subscribers agree on.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from tabvis.gateway import PROTOCOL


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventType:
    """The minimum durable event catalog (design §14.2). Names are the wire ``type`` field."""

    # gateway
    GATEWAY_READY = "gateway.ready"
    GATEWAY_DRAINING = "gateway.draining"
    # conversation
    CONVERSATION_CREATED = "conversation.created"
    CONVERSATION_MESSAGE_RECEIVED = "conversation.message.received"
    # session
    SESSION_CREATED = "session.created"
    SESSION_COMPACTION_COMPLETED = "session.compaction.completed"
    SESSION_FORKED = "session.forked"
    # agent (durable aggregate, design §7.2)
    AGENT_CREATED = "agent.created"
    AGENT_UPDATED = "agent.updated"
    AGENT_DISABLED = "agent.disabled"
    AGENT_DELETED = "agent.deleted"
    # run
    RUN_CREATED = "run.created"
    RUN_QUEUED = "run.queued"
    RUN_STARTED = "run.started"
    RUN_WAITING = "run.waiting"
    RUN_RETRYING = "run.retrying"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"
    # assistant / tools
    ASSISTANT_MESSAGE_COMPLETED = "assistant.message.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    # interactions
    INTERACTION_REQUESTED = "interaction.requested"
    INTERACTION_ANSWERED = "interaction.answered"
    INTERACTION_EXPIRED = "interaction.expired"
    # browser
    BROWSER_BINDING_ACQUIRED = "browser.binding.acquired"
    BROWSER_BINDING_RELEASED = "browser.binding.released"
    BROWSER_NAVIGATION_COMPLETED = "browser.navigation.completed"
    BROWSER_DOWNLOAD_COMPLETED = "browser.download.completed"
    # channel delivery
    CHANNEL_DELIVERY_SUCCEEDED = "channel.delivery.succeeded"
    CHANNEL_DELIVERY_FAILED = "channel.delivery.failed"
    # context
    CONTEXT_PACK_BUILT = "context.pack.built"
    # policy
    POLICY_DECISION = "policy.decision"


# Aggregate kinds an event can be scoped to (the ``aggregate.type`` field).
AGGREGATE_GATEWAY = "gateway"
AGGREGATE_AGENT = "agent"
AGGREGATE_RUN = "run"
AGGREGATE_SESSION = "session"
AGGREGATE_CONVERSATION = "conversation"
AGGREGATE_INTERACTION = "interaction"
AGGREGATE_CHANNEL = "channel"
AGGREGATE_BROWSER = "browser"
AGGREGATE_CONTEXT = "context"


@dataclass(frozen=True)
class EventScope:
    """The ids an event is attributable to (design §9.3 ``scope``). All optional except tenant."""

    tenant_id: str = "local"
    agent_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    conversation_id: str | None = None
    workspace_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class EventEnvelope:
    """One immutable fact in the log (design §9.3).

    ``event_id`` is globally unique; ``cursor`` is the global monotonic position; ``seq`` is the
    per-aggregate sequence. ``correlation_id`` links back to the command that caused this, and
    ``causation_id`` to the event that caused it (for causal tracing, design §17).
    """

    event_id: str
    cursor: int
    aggregate_type: str
    aggregate_id: str
    seq: int
    type: str
    scope: EventScope
    data: dict[str, Any] = field(default_factory=dict)
    occurred_at: str = field(default_factory=_utc_now)
    correlation_id: str | None = None
    causation_id: str | None = None
    protocol: str = PROTOCOL

    def to_dict(self) -> dict[str, Any]:
        """The §9.3 wire object."""
        return {
            "protocol": self.protocol,
            "event_id": self.event_id,
            "cursor": _cursor_str(self.cursor),
            "aggregate": {"type": self.aggregate_type, "id": self.aggregate_id},
            "seq": self.seq,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "scope": self.scope.to_dict(),
            "data": self.data,
        }

    def to_sse_frame(self) -> str:
        """The §9.5 SSE frame: ``id:`` is the zero-padded cursor for ``Last-Event-ID`` resume."""
        import json

        return (
            f"id: {_cursor_str(self.cursor)}\n"
            f"event: {self.type}\n"
            f"data: {json.dumps(self.to_dict(), default=str)}\n\n"
        )


def _cursor_str(cursor: int) -> str:
    """Zero-padded so lexical order matches numeric order (design §9.5 shows a 16-wide cursor)."""
    return f"{cursor:016d}"


def parse_cursor(value: str | int | None) -> int:
    """Parse a wire cursor (zero-padded string, plain int, or None) back to an int; None → 0."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return value
    return int(value)
