"""Branded session/agent IDs.

Uses :func:`typing.NewType` to give distinct static types to different kinds of
identifier; at runtime these are plain ``str`` values.
"""

from __future__ import annotations

import re
from typing import NewType

SessionId = NewType("SessionId", str)
"""Uniquely identifies a Tabvis session. Returned by ``get_session_id()``."""

AgentId = NewType("AgentId", str)
"""Uniquely identifies a subagent within a session. Returned by ``create_agent_id()``."""


def as_session_id(id: str) -> SessionId:
    """Cast a raw string to :data:`SessionId`. Prefer ``get_session_id()`` when possible."""
    return SessionId(id)


def as_agent_id(id: str) -> AgentId:
    """Cast a raw string to :data:`AgentId`. Prefer ``create_agent_id()`` when possible."""
    return AgentId(id)


_AGENT_ID_PATTERN = re.compile(r"^a(?:.+-)?[0-9a-f]{16}$")


def to_agent_id(s: str) -> AgentId | None:
    """Validate and brand a string as :data:`AgentId`.

    Matches the format produced by ``create_agent_id()``: ``a`` + optional ``<label>-`` +
    16 hex chars. Returns ``None`` if the string doesn't match (e.g. teammate names).
    """
    return AgentId(s) if _AGENT_ID_PATTERN.match(s) else None
