"""SimpleX Chat channel plugin — envelope parsing, allowlists, read loop, deliver, and lifecycle.

SimpleX is a client-loop channel (a persistent local WebSocket, no webhook), so the tests differ from
the Feishu webhook tests in one key way: the read source is INJECTED. Each end-to-end test feeds the
plugin a fake async source of canned daemon frames and drives it through the real ``ChannelGateway``
inbound pipeline (bind → message event → Run). Sends are captured through a fake client (SimpleX's
outbound is a WebSocket command, not httpx, so there is no MockTransport to point at). Everything runs
on stdlib + asyncio; the ``websockets`` package is never needed because the live socket is bypassed.
"""

from __future__ import annotations

import asyncio
import json

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.simplex import SimpleXChatChannel, SimpleXConfig
from tabvis.channels.plugins.simplex.client import _CORR_PREFIX, SimpleXClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> SimpleXConfig:
    return SimpleXConfig(**kw)


def _dm_frame(
    item_id: int,
    contact_id: int,
    text: str,
    *,
    direction: str = "directRcv",
    content_type: str = "rcvMsgContent",
    msg_type: str = "text",
    corr_id=None,
    name: str = "alice",
) -> dict:
    """A received-DM frame in the newer ``{"corrId","resp":{...}}`` envelope shape."""
    return {
        "corrId": corr_id,
        "resp": {
            "type": "newChatItems",
            "chatItems": [
                {
                    "chatInfo": {
                        "type": "direct",
                        "contact": {
                            "contactId": contact_id,
                            "localDisplayName": name,
                            "profile": {"displayName": name.capitalize()},
                        },
                    },
                    "chatItem": {
                        "chatDir": {"type": direction},
                        "meta": {"itemId": item_id, "itemTs": "2026-07-22T12:00:00Z"},
                        "content": {"type": content_type, "msgContent": {"type": msg_type, "text": text}},
                    },
                }
            ],
        },
    }


def _group_frame(item_id: int, group_id: int, member_id: str, text: str) -> dict:
    return {
        "corrId": None,
        "resp": {
            "type": "newChatItems",
            "chatItems": [
                {
                    "chatInfo": {"type": "group", "groupInfo": {"groupId": group_id}},
                    "chatItem": {
                        "chatDir": {"type": "groupRcv", "groupMember": {"memberId": member_id}},
                        "meta": {"itemId": item_id},
                        "content": {"type": "rcvMsgContent", "msgContent": {"type": "text", "text": text}},
                    },
                }
            ],
        },
    }


def _source(*frames: dict):
    """A fake read source: an async-generator *function* yielding the given frames then stopping."""

    async def gen():
        for frame in frames:
            yield frame

    return gen


class _FakeClient:
    """Stands in for SimpleXClient so deliver tests never open a socket; records sent commands."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.closed = False

    async def send_command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return f"{_CORR_PREFIX}fake"

    async def aclose(self) -> None:
        self.closed = True


def _channel(*, client=None, source=None, **cfg) -> SimpleXChatChannel:
    return SimpleXChatChannel(
        _config(**cfg), client=client if client is not None else _FakeClient(), source=source
    )


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_simplex_text_only_and_unsigned() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "simplex"
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities
    # SimpleX has no persistent webhook to sign, and no incremental/streamed message updates.
    assert ch.manifest.signed_webhooks is False
    assert "stream.incremental" not in ch.manifest.capabilities


# --- _to_inbound (the unit-testable core) ------------------------------------------------------


def test_to_inbound_parses_received_dm_text() -> None:
    ch = _channel()
    msg = ch._to_inbound(_dm_frame(1337, 42, "hello bot"))
    assert msg is not None
    assert msg.text == "hello bot"
    assert msg.external_conversation_id == "42"   # DM chat id is the bare contactId
    assert msg.external_user_id == "42"
    assert msg.external_event_id == "1337"        # itemId → positive dedupe key


def test_to_inbound_handles_top_level_envelope() -> None:
    # Older daemons emit the event at the top level (no "resp" wrapper); it must still parse.
    frame = _dm_frame(2, 7, "flat envelope")["resp"]
    frame["corrId"] = None
    msg = _channel()._to_inbound(frame)
    assert msg is not None and msg.text == "flat envelope" and msg.external_conversation_id == "7"


def test_to_inbound_ignores_own_and_command_echoes() -> None:
    ch = _channel()
    # 1. our own command echo — corrId carries our prefix.
    echo = _dm_frame(3, 42, "reply we sent", corr_id=f"{_CORR_PREFIX}9-1")
    assert ch._to_inbound(echo) is None
    # 2. our own sent message — outbound direction / content, no corrId to catch it.
    own_dir = _dm_frame(4, 42, "sent", direction="directSnd")
    assert ch._to_inbound(own_dir) is None
    own_content = _dm_frame(5, 42, "sent", content_type="sndMsgContent")
    assert ch._to_inbound(own_content) is None


def test_to_inbound_ignores_non_text_and_non_message_events() -> None:
    ch = _channel()
    # non-text content (file/image/voice) is dropped by a text-only channel.
    assert ch._to_inbound(_dm_frame(6, 42, "", msg_type="file")) is None
    # a non-message event type carries no inbound text.
    assert ch._to_inbound({"resp": {"type": "contactRequest", "contactRequest": {}}}) is None


def test_to_inbound_group_gating() -> None:
    # Groups are OFF by default: an empty allowlist drops all group traffic.
    assert _channel()._to_inbound(_group_frame(10, 55, "mem_1", "hi group")) is None
    # A configured groupId is accepted, namespaced, and carries the member as sender.
    ch = _channel(group_allowed=frozenset({"55"}))
    msg = ch._to_inbound(_group_frame(11, 55, "mem_1", "hi group"))
    assert msg is not None
    assert msg.external_conversation_id == "group:55"
    assert msg.external_user_id == "mem_1"
    # "*" opens every group.
    assert _channel(group_allowed=frozenset({"*"}))._to_inbound(_group_frame(12, 99, "m", "yo")) is not None


def test_to_inbound_dm_allowlist() -> None:
    ch = _channel(allowed_users=frozenset({"42"}))
    assert ch._to_inbound(_dm_frame(20, 42, "allowed")) is not None
    assert ch._to_inbound(_dm_frame(21, 43, "blocked")) is None
    # display-name entries are honored too.
    by_name = _channel(allowed_users=frozenset({"alice"}))
    assert by_name._to_inbound(_dm_frame(22, 42, "hi", name="alice")) is not None


# --- end to end through the gateway (injected source) ------------------------------------------


def test_read_loop_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source(_dm_frame(1, 42, "run this"), _dm_frame(2, 42, "and this")))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        await ch._task  # the finite fake source drains fully through the inbound pipeline

        events = get_event_store().read()
        received = [e for e in events if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 2
        assert EventType.CONVERSATION_CREATED in [e.type for e in events]
        # A Run was created for the conversation the two DMs bound to.
        conv_id = received[0].scope.conversation_id
        run_events = [e for e in events if e.scope.conversation_id == conv_id]
        assert any(e.type == EventType.CONVERSATION_MESSAGE_RECEIVED for e in run_events)

    asyncio.run(scenario())


def test_read_loop_dedupes_repeated_item_id() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        # Same itemId delivered twice: the dedupe ledger must collapse it to one message event.
        ch = _channel(source=_source(_dm_frame(7, 42, "hi"), _dm_frame(7, 42, "hi")))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        await ch._task

        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_dm_resolves_chat_id_and_sends_command() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(client=fake, source=_source(_dm_frame(1, 42, "hello")))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        await ch._task  # the inbound DM creates the conversation<->chat binding deliver needs

        conv_id = [
            e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ][0].scope.conversation_id
        receipt = await gw.deliver(
            "ca_simplex",
            OutboundMessage(delivery_id="dlv-1", conversation_id=conv_id, run_id=None, text="done"),
        )
        assert receipt.status == "succeeded"
        assert fake.commands == ["@42 done"]  # DM shorthand, addressed to the contactId

    asyncio.run(scenario())


def test_deliver_group_uses_structured_send_command() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(
            client=fake,
            group_allowed=frozenset({"55"}),
            source=_source(_group_frame(1, 55, "mem_1", "hi")),
        )
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        await ch._task

        conv_id = [
            e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ][0].scope.conversation_id
        receipt = await gw.deliver(
            "ca_simplex",
            OutboundMessage(delivery_id="dlv-g", conversation_id=conv_id, run_id=None, text="reply"),
        )
        assert receipt.status == "succeeded"
        # Group sends MUST use the structured /_send form (the #[id] shorthand silently drops).
        assert len(fake.commands) == 1
        cmd = fake.commands[0]
        assert cmd.startswith("/_send #55 json ")
        assert json.loads(cmd[len("/_send #55 json "):]) == [{"msgContent": {"type": "text", "text": "reply"}}]

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel()
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        receipt = await gw.deliver(
            "ca_simplex",
            OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi"),
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- send client (frame shape, no socket) ------------------------------------------------------


def test_client_wraps_command_in_corr_id_envelope() -> None:
    async def scenario() -> None:
        sent: list[str] = []

        async def sender(frame: str) -> None:
            sent.append(frame)

        client = SimpleXClient(_config(), sender=sender)
        corr_id = await client.send_command("@42 hi")
        assert corr_id.startswith(_CORR_PREFIX)  # our prefix drives the read loop's echo filter
        payload = json.loads(sent[0])
        assert payload["cmd"] == "@42 hi"
        assert payload["corrId"] == corr_id

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle_starts_ready_and_stop_cancels_loop() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        cancelled = asyncio.Event()

        async def blocking_source():
            try:
                await asyncio.Event().wait()  # a live socket that never yields — stays running
            except asyncio.CancelledError:
                cancelled.set()
                raise
            yield  # pragma: no cover - unreachable

        ch = _channel(client=fake, source=blocking_source)
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))

        assert (await ch.health()).status == "stopped"
        await gw.start_plugin("simplex")
        await asyncio.sleep(0)  # let the read loop task actually begin
        assert (await ch.health()).status == "ready"

        await gw.registry.stop("simplex")
        assert (await ch.health()).status == "stopped"
        assert cancelled.is_set()   # stop() cancelled the read loop
        assert fake.closed is True  # stop() closed the send client

    asyncio.run(scenario())


def test_read_loop_crash_degrades_channel() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()

        async def exploding_source():
            raise RuntimeError("socket blew up")
            yield  # pragma: no cover - unreachable

        ch = _channel(source=exploding_source)
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id="ca_simplex", plugin_id="simplex"))
        await gw.start_plugin("simplex")
        await ch._task  # the guarded loop swallows the crash

        assert (await ch.health()).status == "degraded"

    asyncio.run(scenario())


def test_from_env_reads_tabvis_simplex_vars() -> None:
    ch = SimpleXChatChannel.from_env(
        {
            "TABVIS_SIMPLEX_WS_URL": "ws://10.0.0.1:9000/",
            "TABVIS_SIMPLEX_CHANNEL_ACCOUNT_ID": "ca_x",
            "TABVIS_SIMPLEX_ALLOWED_USERS": "42, alice",
            "TABVIS_SIMPLEX_GROUP_ALLOWED": "55,*",
        }
    )
    assert ch._config.ws_url == "ws://10.0.0.1:9000"  # trailing slash stripped
    assert ch._account_id == "ca_x"
    assert ch._config.allowed_users == frozenset({"42", "alice"})
    assert ch._config.group_allowed == frozenset({"55", "*"})
