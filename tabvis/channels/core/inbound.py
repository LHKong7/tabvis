"""The inbound pipeline (design §4.5).

Turns a normalized :class:`InboundMessage` into the internal effects: dedupe by ``external_event_id``,
resolve or create the conversation binding, emit ``conversation.message.received``, and create the Run
— the design §4.5 inbound sequence, minus the transport-specific signature/normalize steps a plugin
owns.

Idempotency is layered so a webhook retry never produces a second Run or a second message event:

1. the ``channel_inbound`` dedupe ledger short-circuits a repeated ``external_event_id``;
2. the Run's ``command_id`` is *derived* from ``(account, external_event_id)`` so, even if step 1 is
   somehow bypassed, ``run.create`` sees the same command and returns the same Run (design §5.5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from tabvis.channels.core.contract import InboundMessage
from tabvis.channels.core.stores import BindingStore
from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.events import AGGREGATE_CONVERSATION, EventScope, EventType
from tabvis.gateway.runtime.orchestrator import RunOrchestrator, get_orchestrator
from tabvis.gateway.store import db


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derived(prefix: str, account_id: str, external_event_id: str) -> str:
    digest = hashlib.sha1(f"{account_id}:{external_event_id}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}{digest}"


@dataclass
class InboundResult:
    conversation_id: str
    message_id: str
    run_id: str | None
    duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "duplicate": self.duplicate,
        }


class ChannelIngress:
    def __init__(
        self,
        orchestrator: RunOrchestrator | None = None,
        events: EventStore | None = None,
        binding_store: BindingStore | None = None,
    ) -> None:
        self._orch = orchestrator or get_orchestrator()
        self._events = events or get_event_store()
        self._bindings = binding_store or BindingStore()

    async def ingest(self, channel_account_id: str, message: InboundMessage) -> InboundResult:
        # 1. Dedupe by external event id (design §4.5 step 2) — a retry returns the original result.
        prior = db.get_inbound(channel_account_id, message.external_event_id)
        if prior is not None:
            return InboundResult(
                conversation_id=prior["conversation_id"],
                message_id=prior["message_id"],
                run_id=prior["run_id"],
                duplicate=True,
            )

        # 2. Resolve or create the conversation binding (design §4.5 step 5).
        resolution = self._bindings.resolve_or_create(channel_account_id, message.external_conversation_id)
        binding = resolution.binding
        scope = EventScope(
            agent_id=binding.agent_id, session_id=binding.session_id, conversation_id=binding.conversation_id,
        )
        if resolution.created:
            self._events.append(
                AGGREGATE_CONVERSATION, binding.conversation_id, EventType.CONVERSATION_CREATED,
                scope=scope, data={"channel_account_id": channel_account_id,
                                   "external_conversation_id": message.external_conversation_id},
            )

        # 3. Emit conversation.message.received (design §4.5 step 6).
        message_id = _derived("msg_", channel_account_id, message.external_event_id)
        self._events.append(
            AGGREGATE_CONVERSATION, binding.conversation_id, EventType.CONVERSATION_MESSAGE_RECEIVED,
            scope=scope,
            data={
                "message_id": message_id,
                "text": message.text,
                "channel_account_id": channel_account_id,
                "external_user_id": message.external_user_id,
                "attachments": [a.ref for a in message.attachments],
            },
        )

        # 4. Create the Run (design §4.5 step 6). command_id is derived → run.create is idempotent.
        command_id = _derived("cmd_", channel_account_id, message.external_event_id)
        run = await self._orch.create_and_start(
            agent_id=binding.agent_id or "",
            session_id=binding.session_id or "",
            command_id=command_id,
            conversation_id=binding.conversation_id,
            prompt_message_id=message_id,
            prompt=message.text,   # the channel message text is the Run's prompt
        )

        db.record_inbound(
            channel_account_id, message.external_event_id,
            conversation_id=binding.conversation_id, run_id=run.run_id,
            message_id=message_id, created_at=_utc_now(),
        )
        return InboundResult(binding.conversation_id, message_id, run.run_id, duplicate=False)
