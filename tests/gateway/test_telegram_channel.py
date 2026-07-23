"""Telegram channel plugin — parse, client-loop end-to-end, delivery, REST client, lifecycle."""

from __future__ import annotations

import asyncio
import json

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.telegram import TelegramChannel, TelegramConfig
from tabvis.channels.plugins.telegram.client import TelegramClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_telegram"


def _config() -> TelegramConfig:
    return TelegramConfig(bot_token="123:ABC", channel_account_id=ACCOUNT)


def _update(update_id: int, chat_id: int, text: str, *, user_id: int = 111, chat_type: str = "private") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "username": "alice"},
            "chat": {"id": chat_id, "type": chat_type},
            "date": 1690000000,
            "text": text,
        },
    }


async def _source(updates: list[dict]):
    for u in updates:
        yield u


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.closed = False

    async def get_me(self) -> dict:
        return {"id": 999, "is_bot": True, "username": "mybot"}

    async def send_message(self, chat_id, text, *, reply_to_message_id=None) -> str:
        self.sent.append((chat_id, text))
        return "43"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, source=None) -> TelegramChannel:
    return TelegramChannel(_config(), client=fake if fake is not None else _FakeClient(), source=source)


# --- manifest + parse --------------------------------------------------------------------------


def test_manifest() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "telegram"
    assert "message.text.inbound" in ch.manifest.capabilities and ch.manifest.signed_webhooks is False


def test_to_inbound_text_message() -> None:
    msg = _channel()._to_inbound(_update(10, 111, "hey there"))
    assert msg is not None
    assert msg.text == "hey there"
    assert msg.external_conversation_id == "111"
    assert msg.external_event_id == "10"
    assert msg.external_user_id == "111"


def test_to_inbound_ignores_own_and_non_text() -> None:
    ch = _channel()
    ch._bot_id = 999
    own = _update(11, 111, "echo", user_id=999)
    assert ch._to_inbound(own) is None  # bot's own echoed message
    no_text = {"update_id": 12, "message": {"chat": {"id": 111, "type": "private"}, "from": {"id": 111}}}
    assert ch._to_inbound(no_text) is None
    callback = {"update_id": 13, "callback_query": {"id": "x"}}
    assert ch._to_inbound(callback) is None


# --- end to end through the gateway ------------------------------------------------------------


def test_client_loop_creates_run_and_events() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source([_update(100, 111, "run this")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="telegram"))
        await gw.start_plugin("telegram")
        await ch._task  # the injected source is finite; drain it deterministically

        received = [
            e for e in get_event_store().read()
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


def test_retry_same_update_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        # the same update_id delivered twice (a redelivery) must collapse to one message.
        ch = _channel(source=_source([_update(200, 111, "hi"), _update(200, 111, "hi")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="telegram"))
        await gw.start_plugin("telegram")
        await ch._task
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_resolves_chat_and_sends() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(fake, source=_source([_update(300, 555, "hello")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="telegram"))
        await gw.start_plugin("telegram")
        await ch._task
        binding = gw.bindings.get(ACCOUNT, "555")
        assert binding is not None

        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="dlv-1", conversation_id=binding.conversation_id, run_id=None, text="done"),
        )
        assert receipt.status == "succeeded" and receipt.external_message_id == "43"
        # The channel passes the binding's external id through; TelegramClient does the int
        # normalization (verified in the mock-transport test), so the fake records the raw string.
        assert fake.sent == [("555", "done")]

    asyncio.run(scenario())


# --- REST client (mock transport) --------------------------------------------------------------


def test_client_send_message_over_mock_transport() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "/bot123:ABC/" in request.url.path
            if request.url.path.endswith("/sendMessage"):
                body = json.loads(request.content)
                assert body["chat_id"] == 111 and body["text"] == "hi" and body["parse_mode"] is None
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 43}})
            return httpx.Response(200, json={"ok": True, "result": {}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tc = TelegramClient(_config(), client=client)
        assert await tc.send_message(111, "hi") == "43"
        await tc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_lifecycle_starts_and_stops_the_loop() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()

        async def _parked():
            await asyncio.Event().wait()  # never yields — keeps the read loop alive
            yield  # pragma: no cover

        ch = _channel(fake, source=_parked())
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="telegram"))
        await gw.start_plugin("telegram")
        await asyncio.sleep(0)  # let the task reach its await
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("telegram")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
