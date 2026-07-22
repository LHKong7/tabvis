"""The Channel contract and capability vocabulary (design §4.2, §4.4).

A ``ChannelPlugin`` converts an external conversation to normalized inbound messages and gateway
outbound deliveries to the channel's format — nothing more (design §4.1). ``ChannelServices`` is the
narrow, capability-scoped facade the gateway hands a plugin: submit inbound, read a binding, publish a
delivery, resolve a secret, evaluate policy (design §4.2). A plugin never touches the stores directly.

Capabilities (design §4.4) are explicit strings the gateway reads to pick a delivery strategy — e.g.
a channel without ``stream.incremental`` receives throttled updates or only a final response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# --- capability vocabulary (design §4.4) -------------------------------------------------------

CAP_TEXT_INBOUND = "message.text.inbound"
CAP_TEXT_OUTBOUND = "message.text.outbound"
CAP_RICH_OUTBOUND = "message.rich.outbound"
CAP_MESSAGE_UPDATE = "message.update"
CAP_ATTACHMENT_INBOUND = "attachment.inbound"
CAP_INTERACTION_BUTTONS = "interaction.buttons"
CAP_INTERACTION_FORM = "interaction.form"
CAP_STREAM_INCREMENTAL = "stream.incremental"
CAP_THREAD_NATIVE = "thread.native"

ALL_CAPABILITIES = frozenset(
    {
        CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND, CAP_RICH_OUTBOUND, CAP_MESSAGE_UPDATE,
        CAP_ATTACHMENT_INBOUND, CAP_INTERACTION_BUTTONS, CAP_INTERACTION_FORM,
        CAP_STREAM_INCREMENTAL, CAP_THREAD_NATIVE,
    }
)


# --- data carried across the contract ----------------------------------------------------------


@dataclass
class ChannelManifest:
    """Static description of a channel plugin (design §4.2)."""

    plugin_id: str
    version: str
    capabilities: frozenset[str] = field(default_factory=frozenset)
    signed_webhooks: bool = False


@dataclass
class Attachment:
    """An inbound attachment reference — stored before Agent access (design §4.7)."""

    name: str
    media_type: str
    size_bytes: int
    ref: str  # a stored artifact reference, never inline bytes


@dataclass
class RawInbound:
    """What a channel receives from its transport before normalization (design §4.2)."""

    external_event_id: str
    external_conversation_id: str
    external_account_ref: str
    payload: dict[str, Any] = field(default_factory=dict)
    signature: str | None = None
    raw_body: bytes | None = None


@dataclass
class InboundMessage:
    """A normalized inbound message (design §4.2, §4.5). No channel-specific shape survives here."""

    external_event_id: str
    external_conversation_id: str
    external_account_ref: str
    text: str
    external_user_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class OutboundMessage:
    """A gateway → channel delivery (design §4.5)."""

    delivery_id: str
    conversation_id: str
    run_id: str | None
    text: str
    final: bool = True
    channel_account_id: str | None = None


@dataclass
class DeliveryReceipt:
    """The outcome of a delivery attempt (design §4.5, §9.4)."""

    delivery_id: str
    status: str  # succeeded | failed | skipped | duplicate
    external_message_id: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "status": self.status,
            "external_message_id": self.external_message_id,
            "detail": self.detail,
        }


@dataclass
class ChannelHealth:
    status: str  # ready | degraded | stopped
    detail: str | None = None


# --- protocols ---------------------------------------------------------------------------------


class ChannelServices(Protocol):
    """The narrow facade a plugin is given (design §4.2). No direct store access."""

    def submit_inbound(self, channel_account_id: str, message: InboundMessage) -> Any: ...
    def get_binding(self, channel_account_id: str, external_conversation_id: str) -> Any: ...
    def resolve_external_conversation(self, conversation_id: str) -> str | None: ...
    def publish_delivery(self, message: OutboundMessage) -> DeliveryReceipt: ...
    def resolve_secret(self, credential_ref: str) -> str | None: ...


class ChannelPlugin(Protocol):
    """A channel implementation (design §4.2)."""

    manifest: ChannelManifest

    async def start(self, services: ChannelServices) -> None: ...
    async def stop(self) -> None: ...
    async def health(self) -> ChannelHealth: ...
    async def normalize(self, inbound: RawInbound) -> list[InboundMessage]: ...
    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt: ...
    async def acknowledge(self, external_event_id: str) -> None: ...
