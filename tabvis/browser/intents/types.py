"""Intent + Execution primitives (INT-1).

The vocabulary the Intent Router / Execution Engine are built from. An :class:`Intent` is a semantic
verb + params; an :class:`IntentContext` carries the run's ids and mints the ``execution_id`` that
correlates everything the execution produces (``design.md`` §"Intent Router"); an
:class:`ExecutionRecord` is the outcome of running one intent.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from tabvis.browser.session import utc_now


class IntentName:
    """Semantic intent names (``design.md`` §4). Only ``NAVIGATE`` has a handler in INT-1."""

    NAVIGATE = "navigate"
    SEARCH = "search"
    RESEARCH = "research"
    DOWNLOAD = "download"
    COMPARE = "compare"


def new_execution_id() -> str:
    """A unique id for one intent execution (``design.md`` Intent Router assigns one per execution)."""
    return f"exec_{uuid.uuid4().hex[:16]}"


class IntentBlocked(Exception):
    """A handler's PolicyCheck refused the intent (e.g. a navigation outside the allowlist)."""


@dataclass
class Intent:
    """A semantic action the agent wants performed — a verb plus its parameters."""

    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntentContext:
    """Run context for one execution — the ids the design uses to correlate events/artifacts/errors."""

    agent_id: str | None = None
    workspace_id: str | None = None
    execution_id: str = field(default_factory=new_execution_id)


@dataclass
class ExecutionRecord:
    """The outcome of running one intent through the Execution Engine."""

    execution_id: str
    intent: str
    status: str = "running"  # running | completed | failed | blocked
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    observation: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
