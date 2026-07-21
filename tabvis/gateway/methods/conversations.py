"""Conversation command handler (design §9.4, §6.1).

A Conversation is the stable, channel-facing thread. This minimal handler mints a conversation id and
emits ``conversation.created``; a dedicated conversations table and channel binding land with the
Channel Framework (Phase 4). The event is durable now, so the log is complete from the start.
"""

from __future__ import annotations

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.commands import Command, CommandResult, CommandType
from tabvis.gateway.protocol.events import AGGREGATE_CONVERSATION, EventScope, EventType


class ConversationCreateHandler:
    command_type = CommandType.CONVERSATION_CREATE

    def __init__(self, events: EventStore | None = None) -> None:
        self._events = events or get_event_store()

    async def handle(self, command: Command, ctx: CommandContext) -> CommandResult:
        conversation_id = command.data.get("conversation_id") or ids.new_conversation_id()
        title = command.data.get("title")
        self._events.append(
            AGGREGATE_CONVERSATION,
            conversation_id,
            EventType.CONVERSATION_CREATED,
            scope=EventScope(conversation_id=conversation_id),
            data={"title": title, "created_by": ctx.principal.principal_id},
            correlation_id=command.command_id,
        )
        return CommandResult(command.command_id, data={"conversation": {"conversation_id": conversation_id, "title": title}})
