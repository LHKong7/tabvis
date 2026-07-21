"""ChannelAccount and ConversationBinding (design §4.3).

A ``ChannelAccount`` is one configured external connection (a Slack workspace, a webhook endpoint). A
``ConversationBinding`` maps an external thread to an internal conversation; its unique key
``(channel_account_id, external_conversation_id)`` — enforced in SQLite — is what stops a webhook retry
from spawning a second internal conversation (design §4.3).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


@dataclass
class ChannelAccount:
    channel_account_id: str
    plugin_id: str
    tenant_id: str = "local"
    external_account_ref: str = ""
    credential_ref: str = ""  # a SecretStore reference, never the secret itself (design §4.7)
    status: str = "configured"  # configured | starting | ready | degraded | stopped
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelAccount":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class ConversationBinding:
    binding_id: str
    channel_account_id: str
    external_conversation_id: str
    conversation_id: str
    session_id: str | None = None
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationBinding":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
