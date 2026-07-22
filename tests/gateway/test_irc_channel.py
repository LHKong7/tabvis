"""IRC channel plugin — line parse, addressing, client-loop end-to-end, delivery, lifecycle."""

from __future__ import annotations

import asyncio

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.irc import IrcChannel, IrcConfig
from tabvis.channels.plugins.irc.client import extract_nick, parse_irc_message
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_irc"
NICK = "tabvis"


def _config() -> IrcConfig:
    return IrcConfig(server="irc.example.org", channels=("#tabvis",), nickname=NICK, channel_account_id=ACCOUNT)


async def _source(lines: list[str]):
    for line in lines:
        yield line


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.current_nick = NICK
        self.closed = False

    async def send_line(self, line: str) -> None:
        self.sent.append(line)

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeConn | None = None, source=None) -> IrcChannel:
    return IrcChannel(_config(), client=fake if fake is not None else _FakeConn(), source=source)


# --- pure parse --------------------------------------------------------------------------------


def test_parse_and_nick() -> None:
    parsed = parse_irc_message(":alice!alice@host PRIVMSG #tabvis :tabvis: hello there\r\n")
    assert parsed["command"] == "PRIVMSG"
    assert parsed["params"] == ["#tabvis", "tabvis: hello there"]
    assert extract_nick(parsed["prefix"]) == "alice"


def test_to_inbound_channel_requires_addressing() -> None:
    ch = _channel()
    addressed = ch._to_inbound(":alice!a@h PRIVMSG #tabvis :tabvis: what's up")
    assert addressed is not None and addressed.text == "what's up"
    assert addressed.external_conversation_id == "#tabvis" and addressed.external_user_id == "alice"
    # unaddressed channel chatter is ignored
    assert ch._to_inbound(":bob!b@h PRIVMSG #tabvis :just talking") is None
    # own message ignored
    assert ch._to_inbound(":tabvis!t@h PRIVMSG #tabvis :tabvis: me") is None


def test_to_inbound_dm_targets_sender() -> None:
    # a DM's PRIVMSG target is the bot's own nick; the conversation is the SENDER's nick.
    msg = _channel()._to_inbound(":carol!c@h PRIVMSG tabvis :hi there")
    assert msg is not None and msg.external_conversation_id == "carol" and msg.text == "hi there"


# --- end to end + keepalive --------------------------------------------------------------------


def test_client_loop_answers_ping_and_creates_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeConn()
        ch = _channel(fake, source=_source([
            "PING :server1\r\n",
            ":dave!d@h PRIVMSG #tabvis :tabvis: run this\r\n",
        ]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="irc"))
        await gw.start_plugin("irc")
        await ch._task
        assert "PONG :server1" in fake.sent  # keepalive answered
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_deliver_writes_privmsg() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeConn()
        ch = _channel(fake, source=_source([":erin!e@h PRIVMSG #tabvis :tabvis: hi"]))
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="irc"))
        await gw.start_plugin("irc")
        await ch._task
        binding = gw.bindings.get(ACCOUNT, "#tabvis")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="d1", conversation_id=binding.conversation_id, run_id=None, text="the answer"),
        )
        assert receipt.status == "succeeded"
        assert "PRIVMSG #tabvis :the answer" in fake.sent

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
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="irc"))
        await gw.start_plugin("irc")
        await asyncio.sleep(0)
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("irc")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True

    asyncio.run(scenario())
