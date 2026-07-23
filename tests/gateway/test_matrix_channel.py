"""Matrix channel plugin — parse, client-loop end-to-end, delivery, REST client, lifecycle."""

from __future__ import annotations

import asyncio

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.matrix import MatrixChannel, MatrixConfig
from tabvis.channels.plugins.matrix.client import MatrixClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_matrix"
BOT = "@bot:example.org"


def _config() -> MatrixConfig:
    return MatrixConfig(homeserver="https://hs.example.org", access_token="tok", user_id=BOT, channel_account_id=ACCOUNT)


def _event(event_id: str, room_id: str, text: str, *, sender: str = "@alice:example.org", msgtype: str = "m.text") -> dict:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "room_id": room_id,
        "origin_server_ts": 1690000000000,
        "content": {"msgtype": msgtype, "body": text},
    }


async def _source(events: list[dict]):
    for e in events:
        yield e


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.closed = False

    async def whoami(self) -> str:
        return BOT

    async def send_text(self, room_id, text, *, txn_id) -> str:
        self.sent.append((room_id, text, txn_id))
        return "$sent:example.org"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, source=None) -> MatrixChannel:
    return MatrixChannel(_config(), client=fake if fake is not None else _FakeClient(), source=source)


def test_manifest() -> None:
    assert _channel().manifest.plugin_id == "matrix"


def test_to_inbound_and_skips() -> None:
    ch = _channel()
    msg = ch._to_inbound(_event("$1", "!room:example.org", "hello"))
    assert msg is not None and msg.external_conversation_id == "!room:example.org"
    assert msg.external_event_id == "$1" and msg.external_user_id == "@alice:example.org"
    # own message
    assert ch._to_inbound(_event("$2", "!room:example.org", "echo", sender=BOT)) is None
    # notice (not m.text)
    assert ch._to_inbound(_event("$3", "!room:example.org", "n", msgtype="m.notice")) is None
    # an edit
    edit = _event("$4", "!room:example.org", "e")
    edit["content"]["m.relates_to"] = {"rel_type": "m.replace"}
    assert ch._to_inbound(edit) is None


def test_client_loop_creates_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source([_event("$evt1", "!roomA:example.org", "run this")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="matrix"))
        await gw.start_plugin("matrix")
        await ch._task
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_resolves_room_and_sends() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(fake, source=_source([_event("$e", "!roomB:example.org", "hi")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="matrix"))
        await gw.start_plugin("matrix")
        await ch._task
        binding = gw.bindings.get(ACCOUNT, "!roomB:example.org")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="dlv-9", conversation_id=binding.conversation_id, run_id=None, text="done"),
        )
        assert receipt.status == "succeeded" and receipt.external_message_id == "$sent:example.org"
        assert fake.sent == [("!roomB:example.org", "done", "dlv-9")]  # txn id == delivery id

    asyncio.run(scenario())


def test_client_send_over_mock_transport() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == "Bearer tok"
            if "/send/m.room.message/" in request.url.path:
                assert "%21roomC" in str(request.url)  # room id URL-encoded on the wire (raw path)
                return httpx.Response(200, json={"event_id": "$ok:example.org"})
            return httpx.Response(200, json={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        mc = MatrixClient(_config(), client=client)
        assert await mc.send_text("!roomC:example.org", "hi", txn_id="t1") == "$ok:example.org"
        await mc.aclose()

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
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="matrix"))
        await gw.start_plugin("matrix")
        await asyncio.sleep(0)
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("matrix")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
