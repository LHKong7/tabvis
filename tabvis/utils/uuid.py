"""UUID validation + agent-ID minting

The TS module pairs a regex UUID validator with ``createAgentId``, which mints a
task-ID-compatible identifier from 8 random bytes. The TS ``UUID``/``AgentId`` brands
are compile-time only; here ``validate_uuid`` returns a plain ``str`` (the branded
:data:`tabvis.types.ids.AgentId` is reused for the agent-ID return).

Casing: Python identifiers are snake_case. No wire-key dicts cross this boundary, so
the returned values are plain strings.

Implementation note: the TS file imports ``randomBytes`` from Node ``crypto``. The
stdlib equivalent is :mod:`secrets` (``token_hex(8)`` == 8 random bytes rendered as 16
lowercase hex chars) — cryptographically strong and identical in shape to
``randomBytes(8).toString('hex')``. The module name ``tabvis.utils.uuid`` does NOT shadow
the stdlib ``uuid`` (Python 3 absolute imports); this module simply has no need for it.
"""

from __future__ import annotations

import re
import secrets

from tabvis.types.ids import AgentId

# UUID format: 8-4-4-4-12 hex digits (case-insensitive), matching the TS ``uuidRegex``.
_UUID_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def validate_uuid(maybe_uuid: object) -> str | None:
    """Validate that ``maybe_uuid`` is a canonically-formatted UUID string.

    Returns the string itself when it matches the 8-4-4-4-12 hex layout (case
    insensitive), otherwise ``None``. Non-string inputs return ``None`` (parity with
    the TS ``typeof maybeUuid !== 'string'`` guard).
    """
    if not isinstance(maybe_uuid, str):
        return None
    return maybe_uuid if _UUID_REGEX.match(maybe_uuid) else None


def create_agent_id(label: str | None = None) -> AgentId:
    """Generate a new agent ID with an optional label prefix.

    Mirrors the TS ``createAgentId``: 8 random bytes rendered as 16 lowercase hex
    chars, prefixed with ``a`` and an optional ``{label}-`` segment, for consistency
    with task IDs.

    Format: ``a{label-}{16 hex chars}``.
    Example: ``aa3f2c1b4d5e6f7a8`` (no label), ``acompact-a3f2c1b4d5e6f7a8`` (label).
    """
    suffix = secrets.token_hex(8)
    return AgentId(f"a{label}-{suffix}" if label else f"a{suffix}")
