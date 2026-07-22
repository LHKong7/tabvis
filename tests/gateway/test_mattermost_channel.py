"""Mattermost channel plugin — inbound parse, the client-loop end-to-end, delivery, and lifecycle.

Mattermost is a *client-loop* channel (a persistent WebSocket), not a webhook, so the tests drive it
the way :class:`ClientLoopChannel` is meant to be driven: an **injected fake source** of raw ``posted``
events feeds :meth:`_run_loop`, which pushes each parsed message through the real ``ChannelGateway``
inbound pipeline (bind → message event → Run). The platform-specific ``_to_inbound`` parse is also
tested directly. Send is exercised over an ``httpx.MockTransport`` (no network). Uses
``asyncio.run(scenario())`` like the Feishu tests.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from tabvis.channels.core.contract import (
    CAP_STREAM_INCREMENTAL,
    CAP_TEXT_INBOUND,
    CAP_TEXT_OUTBOUND,
    OutboundMessage,
)
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.mattermost import MattermostChannel, MattermostConfig
from tabvis.channels.plugins.mattermost.client import MattermostClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import AGGREGATE_CONVERSATION, AGGREGATE_RUN
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_mattermost"


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> MattermostConfig:
    base = dict(url="https://mm.example.com/", token="tok_bot")
    base.update(kw)
    return MattermostConfig(**base)


def _posted(
    post_id: str,
    channel_id: str,
    text: str,
    *,
    user_id: str = "user_123",
    channel_type: str = "O",
    sender_name: str = "@alice",
    root_id: str = "",
    post_type: str = "",
) -> dict:
    """Build a raw Mattermost WS `posted` envelope — note the double-encoded post string at data.post."""
    post: dict = {
        "id": post_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "message": text,
        "root_id": root_id,
        "file_ids": [],
    }
    if post_type:
        post["type"] = post_type
    return {
        "event": "posted",
        "data": {"post": json.dumps(post), "channel_type": channel_type, "sender_name": sender_name},
    }


async def _fake_source(events):
    """A finite async source of raw events; the read loop drains it and then returns."""
    for ev in events:
        yield ev


class _FakeClient:
    """Stands in for MattermostClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, channel_id: str, text: str, *, root_id: str | None = None) -> str:
        self.calls.append((channel_id, text))
        return "post_sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(*, source=None, client=None, **cfg) -> MattermostChannel:
    return MattermostChannel(_config(**cfg), client=client, source=source)


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_mattermost_and_text_only() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "mattermost"
    assert ch.manifest.signed_webhooks is False  # client-loop: no webhook to sign
    assert CAP_TEXT_INBOUND in ch.manifest.capabilities
    assert CAP_TEXT_OUTBOUND in ch.manifest.capabilities
    # Outbound is a fresh POST per message — no incremental (post-edit) streaming is claimed.
    assert CAP_STREAM_INCREMENTAL not in ch.manifest.capabilities


# --- config ------------------------------------------------------------------------------------


def test_config_from_env_and_websocket_url() -> None:
    cfg = MattermostConfig.from_env(
        {"TABVIS_MATTERMOST_URL": "https://mm.example.com/", "TABVIS_MATTERMOST_TOKEN": "tok"}
    )
    assert cfg.base_url == "https://mm.example.com"  # trailing slash stripped
    assert cfg.token == "tok"
    assert cfg.channel_account_id == "ca_mattermost"  # default account id
    # https → wss for the live WS endpoint.
    assert cfg.websocket_url == "wss://mm.example.com/api/v4/websocket"
    # http → ws too.
    plain = MattermostConfig(url="http://localhost:8065", token="t")
    assert plain.websocket_url == "ws://localhost:8065/api/v4/websocket"


# --- _to_inbound: the platform parse -----------------------------------------------------------


def test_to_inbound_parses_text_and_strips_mention() -> None:
    ch = _channel(bot_username="hermes-bot", bot_user_id="user_bot")
    raw = _posted("post_abc", "chan_456", "@hermes-bot Hello from Matrix!", user_id="user_123")
    msg = ch._to_inbound(raw)
    assert msg is not None
    assert msg.text == "Hello from Matrix!"                 # mention token stripped
    assert msg.external_event_id == "post_abc"             # post.id → dedupe key
    assert msg.external_conversation_id == "chan_456"
    assert msg.external_user_id == "user_123"
    assert msg.external_account_ref == ACCOUNT


def test_to_inbound_ignores_bot_own_message() -> None:
    ch = _channel(bot_user_id="user_bot")
    raw = _posted("post_self", "chan_1", "loop?", user_id="user_bot")
    assert ch._to_inbound(raw) is None


def test_to_inbound_ignores_non_text_and_non_posted() -> None:
    ch = _channel()
    # A non-`posted` event (typing/status/…) — not a message.
    assert ch._to_inbound({"event": "typing", "data": {}}) is None
    # A system post (join/leave/header change) carries a truthy `type`.
    system = _posted("post_sys", "chan_1", "alice joined", post_type="system_join_channel")
    assert ch._to_inbound(system) is None
    # An empty / attachment-only post yields no prompt.
    empty = _posted("post_empty", "chan_1", "")
    assert ch._to_inbound(empty) is None
    # A garbage frame is ignored, not raised.
    assert ch._to_inbound({"event": "posted", "data": {"post": "not json"}}) is None


def test_to_inbound_dm_needs_no_mention() -> None:
    ch = _channel(bot_username="hermes-bot")
    raw = _posted("post_dm", "chan_dm", "what is 2+2", channel_type="D")
    msg = ch._to_inbound(raw)
    assert msg is not None and msg.text == "what is 2+2"


# --- end to end through the gateway (injected fake source) --------------------------------------


def _register(gw: ChannelGateway, ch: MattermostChannel) -> None:
    gw.register_plugin(ch)
    gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="mattermost"))


def test_run_loop_creates_message_events_and_runs() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        events = [
            _posted("post_1", "chan_A", "run this"),
            _posted("post_2", "chan_B", "and this"),
        ]
        ch = _channel(source=_fake_source(events), client=_FakeClient())
        _register(gw, ch)
        await gw.start_plugin("mattermost")
        await ch._task  # finite source → the read loop drains both events, then returns

        conv_types = [
            e.type for e in get_event_store().read(aggregate_type=AGGREGATE_CONVERSATION)
        ]
        assert conv_types.count(EventType.CONVERSATION_MESSAGE_RECEIVED) == 2
        assert EventType.CONVERSATION_CREATED in conv_types

        run_events = get_event_store().read(aggregate_type=AGGREGATE_RUN)
        assert sum(1 for e in run_events if e.type == EventType.RUN_CREATED) == 2

    asyncio.run(scenario())


def test_run_loop_dedupes_repeated_post_id() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        # Same post.id twice (a reconnect can re-deliver) → one message, one Run.
        events = [_posted("post_dup", "chan_C", "hi"), _posted("post_dup", "chan_C", "hi")]
        ch = _channel(source=_fake_source(events), client=_FakeClient())
        _register(gw, ch)
        await gw.start_plugin("mattermost")
        await ch._task

        received = [
            e for e in get_event_store().read(aggregate_type=AGGREGATE_CONVERSATION)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_channel_id_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(source=_fake_source([_posted("post_1", "chan_send", "hello")]), client=fake)
        _register(gw, ch)
        await gw.start_plugin("mattermost")
        await ch._task  # the inbound creates the conversation<->channel binding the outbound needs

        binding = gw.bindings.get(ACCOUNT, "chan_send")
        assert binding is not None
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(
                delivery_id="dlv-1",
                conversation_id=binding.conversation_id,
                run_id=None,
                text="done",
            ),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "post_sent"
        assert fake.calls == [("chan_send", "done")]  # sent to the right channel with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        ch = _channel(source=_fake_source([]), client=_FakeClient())
        _register(gw, ch)
        await gw.start_plugin("mattermost")
        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi"),
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (static bearer + status-based success over a mock transport) ------------------


def test_client_sends_post_with_bearer_and_reads_id() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            assert request.headers["Authorization"] == "Bearer tok_bot"  # static token, no exchange
            body = json.loads(request.content)
            assert body == {"channel_id": "chan_1", "message": "hi there"}
            return httpx.Response(201, json={"id": "post_new", "message": "hi there"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        mc = MattermostClient(_config(), client=client)
        message_id = await mc.send_text("chan_1", "hi there")
        assert message_id == "post_new"
        assert seen == ["/api/v4/posts"]
        await mc.aclose()

    asyncio.run(scenario())


def test_client_raises_on_error_status_even_with_id_in_body() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Mattermost error bodies also carry an `id` — success must key off the status, not `id`.
            return httpx.Response(403, json={"id": "api.context.permissions.app_error", "message": "no"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        mc = MattermostClient(_config(), client=client)
        raised = False
        try:
            await mc.send_text("chan_1", "hi")
        except Exception:  # noqa: BLE001 - ChannelApiError
            raised = True
        assert raised
        await mc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


async def _never_ending():
    """A source that parks forever so the read loop stays alive (health == ready)."""
    gate = asyncio.Event()
    await gate.wait()  # never set → the loop blocks here
    yield {}           # unreachable


def test_plugin_lifecycle_start_ready_stop_stopped() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(source=_never_ending(), client=fake)
        _register(gw, ch)
        assert (await ch.health()).status == "stopped"
        await gw.start_plugin("mattermost")
        await asyncio.sleep(0)  # let the background loop start and park
        assert (await ch.health()).status == "ready"
        await gw.registry.stop("mattermost")
        assert (await ch.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the REST client
        assert ch._task is None      # the read loop was cancelled and cleared

    asyncio.run(scenario())


def test_run_loop_without_source_raises_websocket_hint() -> None:
    async def scenario() -> None:
        # source=None → the live path; without the optional websockets extra it must fail with a hint.
        # The guarded loop swallows the error and marks the channel degraded rather than crashing.
        import builtins

        real_import = builtins.__import__

        def _no_websockets(name, *args, **kwargs):
            if name == "websockets":
                raise ImportError("No module named 'websockets'")
            return real_import(name, *args, **kwargs)

        ch = _channel(source=None, client=_FakeClient())
        builtins.__import__ = _no_websockets
        try:
            raised: list[str] = []
            try:
                await ch._run_loop()
            except RuntimeError as exc:
                raised.append(str(exc))
        finally:
            builtins.__import__ = real_import
        assert raised and "uv sync --extra mattermost" in raised[0]

    asyncio.run(scenario())


if __name__ == "__main__":
    # Convenience: run directly without pytest.
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
