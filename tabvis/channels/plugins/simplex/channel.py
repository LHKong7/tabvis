"""SimpleXChatChannel — a SimpleX Chat channel plugin (design §4.2, §4.8).

SimpleX has no webhook: a single persistent WebSocket to a local ``simplex-chat`` daemon carries
inbound events *and* outbound commands. So this is a :class:`ClientLoopChannel`, not a webhook
plugin — :meth:`start` (inherited) launches :meth:`_run_loop` as a background task that reads the
socket and pushes each received text straight into the inbound pipeline via ``services.submit_inbound``.
``normalize`` is inert (messages never arrive as webhooks); :meth:`_to_inbound` is the unit-testable
core that turns one raw daemon frame into a normalized :class:`InboundMessage` (or ``None`` to skip).

The live socket needs the optional ``websockets`` package (httpx has no WebSocket client); it is
imported lazily so the plugin — and its tests, which inject a fake ``source`` of canned frames — run
without it. See :mod:`tabvis.channels.plugins.simplex.client` for the config and send client.

Wiring sketch::

    simplex = SimpleXChatChannel.from_env()
    gateway.register_plugin(simplex)
    gateway.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
    await gateway.start_plugin("simplex")   # start() spawns the read loop; stop() cancels it
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator, Mapping

from tabvis.channels.core.contract import (
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    ChannelManifest,
    DeliveryReceipt,
    InboundMessage,
    OutboundMessage,
)
from tabvis.channels.plugins._platform.loop import ClientLoopChannel
from tabvis.channels.plugins.simplex.client import (
    _CORR_PREFIX,
    SimpleXClient,
    SimpleXConfig,
    require_websockets,
)

PLUGIN_ID = "simplex"

# Received-message markers. The daemon emits both a corrId-wrapped ("newer") and a top-level ("older")
# envelope shape; we normalize the envelope, then only act on received *text* content.
_MESSAGE_EVENTS = frozenset({"newChatItems", "newChatItem"})
_OWN_DIRECTIONS = frozenset({"directSnd", "groupSnd"})  # our own sends — never react to them
_RECEIVED_CONTENT = "rcvMsgContent"                     # sndMsgContent is our own outbound content


class SimpleXChatChannel(ClientLoopChannel):
    manifest = ChannelManifest(
        plugin_id=PLUGIN_ID,
        version="0.1.0",
        # SimpleX carries plain text both ways; it has no incremental/streamed message updates
        # (no typing indicator, no message edit), so CAP_STREAM_INCREMENTAL is intentionally absent.
        capabilities=frozenset({CAP_TEXT_INBOUND, CAP_TEXT_OUTBOUND}),
        signed_webhooks=False,  # no webhook at all — a persistent socket, no signature to verify
    )

    def __init__(
        self,
        config: SimpleXConfig,
        *,
        client: SimpleXClient | None = None,
        source: Any = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._client = client if client is not None else SimpleXClient(config)
        # The injectable read source: an async iterable, or an async callable returning one, yielding
        # RAW daemon frames. Default (None) builds the live WebSocket in _run_loop. A test passes a
        # fake source of canned frames so the loop is exercised without a real socket.
        self._source = source
        # A client-loop plugin serves exactly one account; start() carries no id, so it is config-driven.
        self._account_id = config.channel_account_id

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SimpleXChatChannel":
        return cls(SimpleXConfig.from_env(env))

    async def stop(self) -> None:
        await super().stop()  # cancel the read loop
        await self._client.aclose()

    # --- read loop -----------------------------------------------------------------------------

    async def _run_loop(self) -> None:
        async for raw in self._events():
            if not isinstance(raw, dict):
                continue
            msg = self._to_inbound(raw)
            if msg is not None:
                await self._submit(self._account_id, msg)

    def _events(self) -> AsyncIterator[dict]:
        """Resolve the configured source to an async iterator of raw frames.

        Accepts an async iterable *or* an async callable returning one (e.g. an async-generator
        function). With no source, builds the live WebSocket stream.
        """
        src = self._source
        if src is None:
            return self._live_events()
        if callable(src) and not hasattr(src, "__aiter__"):
            return src()
        return src

    async def _live_events(self) -> AsyncIterator[dict]:
        """Read the live daemon socket forever, reconnecting with capped backoff.

        Needs the optional ``websockets`` package; without it the import raises a clear hint and the
        guarded loop degrades the channel (never the process). Each frame is one JSON line.
        """
        websockets = require_websockets()
        backoff = 2.0
        while True:
            try:
                async with websockets.connect(
                    self._config.ws_url, ping_interval=20, ping_timeout=20, close_timeout=10
                ) as ws:
                    backoff = 2.0  # a clean connect resets the backoff
                    async for raw in ws:
                        try:
                            frame = json.loads(raw)
                        except (ValueError, TypeError):
                            continue  # a non-JSON line is noise, not a fatal error
                        if isinstance(frame, dict):
                            yield frame
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a dropped socket reconnects; it never kills the loop
                # Exponential backoff 2s→60s with 20% jitter so a flapping daemon isn't hammered.
                await asyncio.sleep(min(backoff, 60.0) * (1.0 + 0.2 * random.random()))
                backoff = min(backoff * 2.0, 60.0)

    # --- event → normalized message ------------------------------------------------------------

    def _to_inbound(self, raw: dict) -> InboundMessage | None:
        """Parse one raw daemon frame into an :class:`InboundMessage`, or ``None`` to skip it.

        Skips: our own command echoes (corrId prefix), our own sent messages (chatDir/content),
        non-text content, non-message events, and traffic from users/groups outside the allowlists.
        This is the plugin's testable heart.
        """
        # Drop the daemon's echo of a command we sent (its corrId carries our prefix).
        corr_id = raw.get("corrId")
        if isinstance(corr_id, str) and corr_id.startswith(_CORR_PREFIX):
            return None

        # Normalize the envelope: newer frames wrap the event under "resp", older ones are top-level.
        resp = raw["resp"] if isinstance(raw.get("resp"), dict) else raw
        resp_type = resp.get("type") or raw.get("type") or ""
        if resp_type not in _MESSAGE_EVENTS:
            return None  # contactRequest / file-transfer / status events carry no inbound text

        items = resp.get("chatItems") if resp_type == "newChatItems" else [resp.get("chatItem")]
        for item in items or []:
            if isinstance(item, dict):
                msg = self._item_to_inbound(item)
                if msg is not None:
                    return msg  # first eligible text item wins (a frame normally carries one)
        return None

    def _item_to_inbound(self, item: dict) -> InboundMessage | None:
        chat_info = item.get("chatInfo") or {}
        chat_item = item.get("chatItem") or {}

        # Own-message guard (belt-and-suspenders for an echo without a matching corrId).
        if (chat_item.get("chatDir") or {}).get("type") in _OWN_DIRECTIONS:
            return None
        content = chat_item.get("content") or {}
        if content.get("type") != _RECEIVED_CONTENT:  # only *received* content, never our sends
            return None
        msg_content = content.get("msgContent") or {}
        if msg_content.get("type") != "text":  # a text-only channel ignores file/image/voice/…
            return None
        text = str(msg_content.get("text") or "")
        if not text:
            return None

        addressing = self._addressing(chat_info, chat_item)
        if addressing is None:  # gated out (allowlist) or an unrecognized chat shape
            return None
        chat_id, sender_id = addressing

        # itemId is a stable per-daemon message id; using it as the event id gives positive dedupe
        # (a re-delivered frame collapses to one Run) rather than relying on the echo filters alone.
        meta = chat_item.get("meta") or {}
        item_id = meta.get("itemId")
        external_event_id = str(item_id) if item_id is not None else f"{chat_id}:{text[:32]}"

        return InboundMessage(
            external_event_id=external_event_id,
            external_conversation_id=chat_id,
            external_account_ref=self._config.ws_url,
            text=text,
            external_user_id=sender_id,
        )

    def _addressing(self, chat_info: dict, chat_item: dict) -> tuple[str, str] | None:
        """Resolve ``(chat_id, sender_id)`` and apply the DM/group allowlists, or ``None`` to drop."""
        chat_type = chat_info.get("type")
        if chat_type == "direct":
            contact = chat_info.get("contact") or {}
            contact_id = str(contact.get("contactId"))
            name = contact.get("localDisplayName") or (contact.get("profile") or {}).get("displayName")
            if not self._dm_allowed(contact_id, name):
                return None
            # For a DM the chat id and sender id are the same bare contactId.
            return contact_id, contact_id
        if chat_type == "group":
            group_id = str((chat_info.get("groupInfo") or {}).get("groupId"))
            if not self._group_allowed(group_id):
                return None
            member = (chat_item.get("chatDir") or {}).get("groupMember") or {}
            sender_id = str(member.get("memberId") or "")
            # Group chat ids are namespaced so outbound addressing (@id vs /_send #id) can tell them apart.
            return f"group:{group_id}", sender_id
        return None

    def _dm_allowed(self, contact_id: str, display_name: str | None) -> bool:
        # An unset allowlist means "open": the local daemon is trusted, so a bare install just works.
        # Configure TABVIS_SIMPLEX_ALLOWED_USERS to restrict, or ALLOW_ALL_USERS to force open.
        if self._config.allow_all_users or not self._config.allowed_users:
            return True
        allow = self._config.allowed_users
        return contact_id in allow or (display_name is not None and display_name in allow)

    def _group_allowed(self, group_id: str) -> bool:
        # Groups are OFF by default: an empty allowlist drops all group traffic (mirrors the reference).
        allow = self._config.group_allowed
        return "*" in allow or group_id in allow

    # --- outbound --------------------------------------------------------------------------------

    async def deliver(self, outbound: OutboundMessage) -> DeliveryReceipt:
        chat_id = self._resolve_chat_id(outbound)
        if not chat_id:
            return DeliveryReceipt(
                outbound.delivery_id, status="failed", detail="no external chat id for conversation"
            )
        try:
            corr_id = await self._client.send_command(_send_command(chat_id, outbound.text))
        except Exception as exc:  # noqa: BLE001 - a send failure is a receipt, never a raise
            return DeliveryReceipt(outbound.delivery_id, status="failed", detail=str(exc))
        # Fire-and-forget: a successful socket write is success; the corrId is our only message handle.
        return DeliveryReceipt(outbound.delivery_id, status="succeeded", external_message_id=corr_id)

    def _resolve_chat_id(self, outbound: OutboundMessage) -> str | None:
        # The gateway hands us an internal conversation_id; the SimpleX chat id is the binding's external id.
        if self._services is None:
            return None
        resolver = getattr(self._services, "resolve_external_conversation", None)
        return resolver(outbound.conversation_id) if resolver is not None else None


def _send_command(chat_id: str, text: str) -> str:
    """Build the SimpleX chat command for a plain-text reply.

    A DM is the terminal shorthand ``@<contactId> <text>``. A group MUST use the structured
    ``/_send #<groupId> json [...]`` form — the ``#[id] text`` bracket shorthand is parsed as a
    display-name lookup and silently drops the message.
    """
    if chat_id.startswith("group:"):
        group_id = chat_id[len("group:"):]
        payload = json.dumps([{"msgContent": {"type": "text", "text": text}}])
        return f"/_send #{group_id} json {payload}"
    return f"@{chat_id} {text}"
