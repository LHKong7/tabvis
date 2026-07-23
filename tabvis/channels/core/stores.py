"""ChannelAccount and ConversationBinding stores (design §4.3).

The binding store's ``resolve_or_create`` is the load-bearing piece: it returns the existing binding
for a known external thread and atomically creates one (minting a fresh conversation/agent/session)
for a new thread. The SQLite ``UNIQUE(channel_account_id, external_conversation_id)`` makes a racing
double-create collapse to a single binding (design §4.3) — the second caller catches the integrity
error and re-reads.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from tabvis.channels.core.identity import ChannelAccount, ConversationBinding
from tabvis.gateway.protocol import ids
from tabvis.gateway.store import db


class ChannelAccountStore:
    def register(self, account: ChannelAccount) -> ChannelAccount:
        db.upsert_channel_account(account.to_dict())
        return account

    def get(self, channel_account_id: str) -> ChannelAccount | None:
        data = db.get_channel_account(channel_account_id)
        return ChannelAccount.from_dict(data) if data else None

    def list(self) -> list[ChannelAccount]:
        return [ChannelAccount.from_dict(d) for d in db.list_channel_accounts()]


@dataclass
class BindingResolution:
    binding: ConversationBinding
    created: bool


class BindingStore:
    def resolve_or_create(self, channel_account_id: str, external_conversation_id: str) -> BindingResolution:
        existing = db.get_binding(channel_account_id, external_conversation_id)
        if existing is not None:
            return BindingResolution(ConversationBinding.from_dict(existing), created=False)

        binding = ConversationBinding(
            binding_id=ids.new_subscription_id().replace("sub_", "bnd_"),
            channel_account_id=channel_account_id,
            external_conversation_id=external_conversation_id,
            conversation_id=ids.new_conversation_id(),
            session_id=ids.new_session_id(),
            agent_id=ids.new_agent_id(),
        )
        try:
            with db.transaction() as conn:
                # Re-check inside the transaction; the UNIQUE key is the final guard.
                inside = db.get_binding_in(conn, channel_account_id, external_conversation_id)
                if inside is not None:
                    return BindingResolution(ConversationBinding.from_dict(inside), created=False)
                db.insert_binding(conn, binding.to_dict())
        except sqlite3.IntegrityError:
            # Lost a create race — the winner's binding is authoritative.
            won = db.get_binding(channel_account_id, external_conversation_id)
            return BindingResolution(ConversationBinding.from_dict(won), created=False)
        return BindingResolution(binding, created=True)

    def get(self, channel_account_id: str, external_conversation_id: str) -> ConversationBinding | None:
        data = db.get_binding(channel_account_id, external_conversation_id)
        return ConversationBinding.from_dict(data) if data else None

    def get_by_conversation(self, conversation_id: str) -> ConversationBinding | None:
        """Reverse lookup used by outbound delivery to recover a channel's external thread id."""
        data = db.get_binding_by_conversation(conversation_id)
        return ConversationBinding.from_dict(data) if data else None
