"""Signal channel plugin — parse, client-loop end-to-end, delivery, lifecycle."""

from __future__ import annotations

import asyncio

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.signal import SignalChannel, SignalConfig
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_signal"
SELF = "+15550000000"


def _config() -> SignalConfig:
    return SignalConfig(account=SELF, channel_account_id=ACCOUNT)


def _receive(text, *, source="+15551112222", group=None, ts=1690000000000) -> dict:
    data_message = {"message": text, "timestamp": ts}
    if group is not None:
        data_message["groupInfo"] = {"groupId": group}
    return {"method": "receive", "params": {"envelope": {"source": source, "sourceNumber": source,
                                                         "timestamp": ts, "dataMessage": data_message}}}


async def _source(messages: list[dict]):
    for m in messages:
        yield m


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.closed = False

    async def send(self, method, params) -> None:
        self.sent.append((method, params))

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake=None, source=None) -> SignalChannel:
    return SignalChannel(_config(), client=fake if fake is not None else _FakeConn(), source=source)


def test_manifest() -> None:
    assert _channel().manifest.plugin_id == "signal"


def test_to_inbound_and_skips() -> None:
    ch = _channel()
    dm = ch._to_inbound(_receive("hello")["params"])
    assert dm is not None and dm.text == "hello" and dm.external_conversation_id == "+15551112222"
    # a group message keys on the group id
    grp = ch._to_inbound(_receive("hey all", group="grp==")["params"])
    assert grp is not None and grp.external_conversation_id == "grp=="
    # our own number (a sync) is ignored
    assert ch._to_inbound(_receive("echo", source=SELF)["params"]) is None
    # a receipt (no dataMessage text) is ignored
    assert ch._to_inbound({"envelope": {"source": "+1", "receiptMessage": {}}}) is None


def test_client_loop_creates_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_source([_receive("run this", source="+1999")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="signal"))
        await gw.start_plugin("signal")
        await ch._task
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_dm_and_group() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeConn()
        ch = _channel(fake, source=_source([_receive("hi", source="+15559990000")]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="signal"))
        await gw.start_plugin("signal")
        await ch._task

        binding = gw.bindings.get(ACCOUNT, "+15559990000")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="d1", conversation_id=binding.conversation_id, run_id=None, text="reply"),
        )
        assert receipt.status == "succeeded"
        assert fake.sent == [("send", {"recipient": ["+15559990000"], "message": "reply"})]  # DM -> recipient

    asyncio.run(scenario())


def test_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeConn()

        async def _parked():
            await asyncio.Event().wait()
            yield  # pragma: no cover

        ch = _channel(fake, source=_parked())
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="signal"))
        await gw.start_plugin("signal")
        await asyncio.sleep(0)
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("signal")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
