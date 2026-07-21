"""Typed, prefixed identifiers (design §15 Phase 0, §9).

Every aggregate and protocol object has its own short, URL-safe, collision-resistant id with a
one-glance prefix — the same style as the existing ``ag_...`` agent id (``registry.new_agent_id``),
extended to the gateway's vocabulary. A prefix makes an id self-describing in a log line and lets a
handler cheaply reject an id of the wrong kind before touching a store.

The mint/validate helpers are deliberately tiny and dependency-free so they can be imported from any
layer. ``token_hex(6)`` gives 48 bits of entropy per id — ample for a single local daemon while
staying compact.
"""

from __future__ import annotations

import secrets
from typing import Final

# --- prefixes, one per aggregate/protocol object -----------------------------------------------
# ``ag_`` is kept identical to the existing agent id so the two id spaces are one (design §7.2).
AGENT_PREFIX: Final = "ag_"
RUN_PREFIX: Final = "run_"
SESSION_PREFIX: Final = "ses_"
CONVERSATION_PREFIX: Final = "conv_"
INTERACTION_PREFIX: Final = "int_"
COMMAND_PREFIX: Final = "cmd_"
EVENT_PREFIX: Final = "evt_"
SUBSCRIPTION_PREFIX: Final = "sub_"
WORKSPACE_PREFIX: Final = "ws_"
DELIVERY_PREFIX: Final = "dlv_"

_ENTROPY_BYTES: Final = 6


def _mint(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(_ENTROPY_BYTES)}"


def has_prefix(value: str, prefix: str) -> bool:
    """True iff ``value`` is a non-empty string carrying ``prefix`` and at least one id character."""
    return isinstance(value, str) and value.startswith(prefix) and len(value) > len(prefix)


# --- minters -----------------------------------------------------------------------------------


def new_agent_id() -> str:
    return _mint(AGENT_PREFIX)


def new_run_id() -> str:
    return _mint(RUN_PREFIX)


def new_session_id() -> str:
    return _mint(SESSION_PREFIX)


def new_conversation_id() -> str:
    return _mint(CONVERSATION_PREFIX)


def new_interaction_id() -> str:
    return _mint(INTERACTION_PREFIX)


def new_command_id() -> str:
    return _mint(COMMAND_PREFIX)


def new_event_id() -> str:
    return _mint(EVENT_PREFIX)


def new_subscription_id() -> str:
    return _mint(SUBSCRIPTION_PREFIX)


def new_workspace_id() -> str:
    return _mint(WORKSPACE_PREFIX)


def new_delivery_id() -> str:
    return _mint(DELIVERY_PREFIX)


# --- validators --------------------------------------------------------------------------------


def is_run_id(value: str) -> bool:
    return has_prefix(value, RUN_PREFIX)


def is_session_id(value: str) -> bool:
    return has_prefix(value, SESSION_PREFIX)


def is_conversation_id(value: str) -> bool:
    return has_prefix(value, CONVERSATION_PREFIX)


def is_interaction_id(value: str) -> bool:
    return has_prefix(value, INTERACTION_PREFIX)


def is_command_id(value: str) -> bool:
    return has_prefix(value, COMMAND_PREFIX)


def is_event_id(value: str) -> bool:
    return has_prefix(value, EVENT_PREFIX)
