"""iMessage channel plugin — parse, client-loop end-to-end, delivery, sidecar client, lifecycle."""

from __future__ import annotations

import asyncio
import json

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.imessage import IMessageChannel, IMessageConfig
from tabvis.channels.plugins.imessage.client import IMessageSidecarClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_imessage"


def _config() -> IMessageConfig:
    return IMessageConfig(sidecar_token="tok", channel_account_id=ACCOUNT)


def _event(message_id, space_id, text, *, sender="+15551234567", ctype="text", direction=None) -> dict:
    evt = {
        "messageId": message_id,
        "platform": "iMessage",
        "space": {"id": space_id, "type": "dm", "phone": sender},
        "sender": {"id": sender},
        "content": {"type": ctype, "text": text},
    }
    if direction is not None:
        evt["direction"] = direction
    return evt


async def _source(events: list[dict]):
    for e in events:
        yield e


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.closed = False

    async def send_text(self, space_id, text, *, fmt=None) -> str:
        self.sent.append((space_id, text, fmt))
        return "spc-out-1"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake=None, source=None) -> IMessageChannel:
    return IMessageChannel(_config(), client=fake if fake is not None else _FakeClient(), source=source)


def test_manifest() -> None:
    assert _channel().manifest.plugin_id == "imessage"


def test_to_inbound_and_skips() -> None:
    ch = _channel()
    msg = ch._to_inbound(_event("m1", "+15551234567", "hello world"))
    assert msg is not None and msg.text == "hello world"
    assert msg.external_conversation_id == "+15551234567" and msg.external_event_id == "m1"
    # our own outbound echoed with a direction marker
    assert ch._to_inbound(_event("m2", "+1555", "echo", direction="outbound")) is None
    # a non-text content type
    assert ch._to_inbound(_event("m3", "+1555", "", ctype="reaction")) is None


def test_client_loop_creates_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source([_event("evt1", "group-guid", "run this", sender="+1999")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="imessage"))
        await gw.start_plugin("imessage")
        await ch._task
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_resolves_space_and_sends() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(fake, source=_source([_event("e", "+15551112222", "hi")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="imessage"))
        await gw.start_plugin("imessage")
        await ch._task
        binding = gw.bindings.get(ACCOUNT, "+15551112222")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="d1", conversation_id=binding.conversation_id, run_id=None, text="done"),
        )
        assert receipt.status == "succeeded" and receipt.external_message_id == "spc-out-1"
        assert fake.sent == [("+15551112222", "done", "markdown")]

    asyncio.run(scenario())


def test_sidecar_client_send_over_mock_transport() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-Hermes-Sidecar-Token"] == "tok"
            body = json.loads(request.content)
            assert body["spaceId"] == "+1555" and body["text"] == "hi"
            return httpx.Response(200, json={"ok": True, "messageId": "spc-9"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sc = IMessageSidecarClient(_config(), client=client)
        assert await sc.send_text("+1555", "hi") == "spc-9"
        await sc.aclose()

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
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="imessage"))
        await gw.start_plugin("imessage")
        await asyncio.sleep(0)
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("imessage")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
