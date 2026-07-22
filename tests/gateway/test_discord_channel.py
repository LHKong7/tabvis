"""Discord channel plugin — parse, client-loop end-to-end, delivery, REST client, lifecycle."""

from __future__ import annotations

import asyncio
import json

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.discord import DiscordChannel, DiscordConfig
from tabvis.channels.plugins.discord.client import DiscordClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_discord"
BOT_ID = "999"


def _config() -> DiscordConfig:
    return DiscordConfig(bot_token="botsecret", bot_user_id=BOT_ID, channel_account_id=ACCOUNT)


def _message(msg_id, channel_id, text, *, author_id="111", bot=False) -> dict:
    return {
        "id": msg_id,
        "channel_id": channel_id,
        "author": {"id": author_id, "username": "alice", "bot": bot},
        "content": text,
    }


async def _source(events: list[dict]):
    for e in events:
        yield e


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.closed = False

    async def send_text(self, channel_id, text) -> str:
        self.sent.append((channel_id, text))
        return "msg-out"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake=None, source=None) -> DiscordChannel:
    return DiscordChannel(_config(), client=fake if fake is not None else _FakeClient(), source=source)


def test_manifest() -> None:
    assert _channel().manifest.plugin_id == "discord"


def test_to_inbound_and_skips() -> None:
    ch = _channel()
    msg = ch._to_inbound(_message("m1", "chan1", "hello"))
    assert msg is not None and msg.external_conversation_id == "chan1" and msg.external_user_id == "111"
    assert ch._to_inbound(_message("m2", "chan1", "echo", author_id=BOT_ID)) is None  # own message
    assert ch._to_inbound(_message("m3", "chan1", "beep", bot=True)) is None  # another bot
    assert ch._to_inbound(_message("m4", "chan1", "")) is None  # empty content (no MESSAGE_CONTENT)


def test_client_loop_creates_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source([_message("evt1", "chanA", "run this")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="discord"))
        await gw.start_plugin("discord")
        await ch._task
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_resolves_channel_and_sends() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(fake, source=_source([_message("e", "chanB", "hi")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="discord"))
        await gw.start_plugin("discord")
        await ch._task
        binding = gw.bindings.get(ACCOUNT, "chanB")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="d1", conversation_id=binding.conversation_id, run_id=None, text="done"),
        )
        assert receipt.status == "succeeded" and receipt.external_message_id == "msg-out"
        assert fake.sent == [("chanB", "done")]

    asyncio.run(scenario())


def test_rest_client_send_over_mock_transport() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bot botsecret"  # `Bot` scheme, not Bearer
            body = json.loads(request.content)
            assert body["content"] == "hi" and body["allowed_mentions"] == {"parse": ["users"]}
            return httpx.Response(200, json={"id": "sent-1"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dc = DiscordClient(_config(), client=client)
        assert await dc.send_text("chanC", "hi") == "sent-1"
        await dc.aclose()

    asyncio.run(scenario())


def test_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()

        async def _parked():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        ch = _channel(fake, source=_parked())
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="discord"))
        await gw.start_plugin("discord")
        await asyncio.sleep(0)
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("discord")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
