"""Tabvis message envelope types

These are the internal session-transcript message shapes (distinct from the Anthropic
wire format). In TS they are loose tagged objects (``{ type, message?, uuid?, ... }`` with
an open ``[key]: unknown`` index signature). The faithful Python mapping is a ``dict`` with
a ``type`` discriminator; the ``TypedDict`` classes below document the known fields while
runtime dicts still accept arbitrary extra keys.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class MessageBase(TypedDict, total=False):
    uuid: str
    parentUuid: str
    timestamp: str
    createdAt: str
    isMeta: bool
    isVirtual: bool
    isCompactSummary: bool
    toolUseResult: Any
    origin: dict[str, Any]
    # Plus arbitrary extra keys ([key: string]: unknown).


class AttachmentMessage(MessageBase, total=False):
    type: Literal["attachment"]
    path: str


class _UserMessagePayload(TypedDict, total=False):
    content: Any  # str | list[{type, text?, ...}]


class UserMessage(MessageBase, total=False):
    type: Literal["user"]
    message: _UserMessagePayload


class AssistantMessage(MessageBase, total=False):
    type: Literal["assistant"]
    message: dict[str, Any]  # { content?, ... }


class ProgressMessage(MessageBase, total=False):
    type: Literal["progress"]
    progress: Any
    data: Any


SystemMessageLevel = Literal["info", "warning", "error"] | str


class SystemMessage(MessageBase, total=False):
    type: Literal["system"]
    subtype: str
    level: SystemMessageLevel
    message: str
    error: str


class HookResultMessage(MessageBase, total=False):
    type: Literal["hook_result"]


class ToolUseSummaryMessage(MessageBase, total=False):
    type: Literal["tool_use_summary"]


class TombstoneMessage(MessageBase, total=False):
    type: Literal["tombstone"]


class GroupedToolUseMessage(MessageBase, total=False):
    type: Literal["grouped_tool_use"]


class StreamEvent(TypedDict, total=False):
    type: str
    # Plus arbitrary extra keys.


RequestStartEvent = StreamEvent

Message = (
    UserMessage
    | AssistantMessage
    | ProgressMessage
    | SystemMessage
    | AttachmentMessage
    | HookResultMessage
    | ToolUseSummaryMessage
    | TombstoneMessage
    | GroupedToolUseMessage
)

NormalizedMessage = (
    AssistantMessage | UserMessage | ProgressMessage | SystemMessage | AttachmentMessage
)
RenderableMessage = Message
