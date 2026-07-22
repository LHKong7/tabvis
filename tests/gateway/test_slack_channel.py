"""Slack IM channel plugin — crypto, webhook decoding, normalize, deliver, and end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus Slack's own webhook
verification (the ``v0:{ts}:{body}`` request signature, its replay guard, and the url_verification
challenge). Inbound is the Events API HTTP-webhook shape (the stdlib+httpx equivalent of the
reference's Socket Mode transport); the event JSON is identical either way.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.slack import SlackChannel, SlackConfig
from tabvis.channels.plugins.slack import crypto
from tabvis.channels.plugins.slack.client import SlackClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> SlackConfig:
    base = dict(bot_token="xoxb-test")
    base.update(kw)
    return SlackConfig(**base)


def _event_callback(
    channel: str,
    text: str,
    *,
    ts: str = "1700000000.000100",
    user: str = "U_USER",
    channel_type: str = "im",
    inner_type: str = "message",
    team_id: str = "T_TEAM",
    event_id: str = "Ev_1",
) -> dict:
    return {
        "type": "event_callback",
        "event_id": event_id,
        "team_id": team_id,
        "api_app_id": "A_APP",
        "event": {
            "type": inner_type,
            "text": text,
            "user": user,
            "channel": channel,
            "channel_type": channel_type,
            "ts": ts,
        },
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _signed_headers(secret: str, body: bytes, *, ts: str | None = None) -> dict[str, str]:
    ts = ts if ts is not None else str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": crypto.slack_signature(secret, ts, body),
    }


class _FakeClient:
    """Stands in for SlackClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, channel: str, text: str, *, thread_ts: str | None = None) -> str:
        self.calls.append((channel, text))
        return "1700000000.000999"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> SlackChannel:
    return SlackChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_slack_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "slack"
    # Slack verifies its own webhooks (custom base-string HMAC), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_roundtrip_and_rejection() -> None:
    secret = "sign-secret"
    body = _body({"type": "event_callback"})
    ts = str(int(time.time()))
    sig = crypto.slack_signature(secret, ts, body)
    assert crypto.verify_signature(secret, ts, body, sig)
    assert not crypto.verify_signature(secret, ts, body, "v0=deadbeef")   # wrong digest
    assert not crypto.verify_signature(secret, ts, body, None)            # missing signature
    assert not crypto.verify_signature("", ts, body, sig)                 # missing secret
    assert not crypto.verify_signature(secret, "", body, sig)             # missing timestamp
    # Replay guard: a valid signature over a stale timestamp is still rejected.
    old_ts = "1"
    old_sig = crypto.slack_signature(secret, old_ts, body)
    assert not crypto.verify_signature(secret, old_ts, body, old_sig)
    # ...but is accepted when evaluated relative to that moment (now injected).
    assert crypto.verify_signature(secret, old_ts, body, old_sig, now=1.0)


# --- webhook decoding: challenge / signature ---------------------------------------------------


def test_handle_webhook_url_verification_returns_challenge() -> None:
    secret = "sign-secret"
    ch = _channel(signing_secret=secret)
    body = _body({"type": "url_verification", "token": "tok", "challenge": "ch-123"})
    result = ch.handle_webhook(_signed_headers(secret, body), body)
    assert result.challenge == "ch-123"
    assert result.raw is None and not result.rejected


def test_handle_webhook_requires_valid_signature() -> None:
    secret = "sign-secret"
    ch = _channel(signing_secret=secret)
    body = _body(_event_callback("D123", "hi"))

    ok = ch.handle_webhook(_signed_headers(secret, body), body)
    assert ok.raw is not None and not ok.rejected
    assert ok.raw.external_event_id == "1700000000.000100"
    assert ok.raw.external_conversation_id == "D123"

    ts = str(int(time.time()))
    bad = ch.handle_webhook(
        {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=nope"}, body
    )
    assert bad.rejected

    missing = ch.handle_webhook({}, body)  # no signature headers at all
    assert missing.rejected


def test_handle_webhook_rejects_stale_timestamp_replay() -> None:
    secret = "sign-secret"
    ch = _channel(signing_secret=secret)
    body = _body(_event_callback("D123", "hi"))
    # A correctly signed request, but the timestamp is ancient → replay window rejects it.
    result = ch.handle_webhook(_signed_headers(secret, body, ts="1"), body)
    assert result.rejected


def test_handle_webhook_rejects_invalid_json() -> None:
    result = _channel().handle_webhook({}, b"not json")
    assert result.rejected


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()  # no signing secret → verification skipped for the normalize helpers
        raw = ch.handle_webhook({}, _body(_event_callback("D123", "hello bot"))).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "D123"
        assert msg.external_event_id == "1700000000.000100"
        assert msg.external_user_id == "U_USER"

    asyncio.run(scenario())


def test_normalize_strips_bot_mention_in_channel() -> None:
    async def scenario() -> None:
        ch = _channel(bot_user_id="U0BOTID")
        event = _event_callback(
            "C123", "<@U0BOTID> do the thing", channel_type="channel", inner_type="app_mention"
        )
        (msg,) = await ch.normalize(ch.handle_webhook({}, _body(event)).raw)
        assert msg.text == "do the thing"

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # a bot's own / another bot's message
        bot_event = _event_callback("C123", "loop?")
        bot_event["event"]["bot_id"] = "B999"
        assert await ch.normalize(ch.handle_webhook({}, _body(bot_event)).raw) == []
        # a bot_message subtype
        subtype_event = _event_callback("C123", "loop?")
        subtype_event["event"]["subtype"] = "bot_message"
        assert await ch.normalize(ch.handle_webhook({}, _body(subtype_event)).raw) == []
        # an edit/delete
        edit_event = _event_callback("C123", "edited")
        edit_event["event"]["subtype"] = "message_changed"
        assert await ch.normalize(ch.handle_webhook({}, _body(edit_event)).raw) == []
        # a non-message event type
        other = _event_callback("C123", "x", inner_type="reaction_added")
        assert await ch.normalize(ch.handle_webhook({}, _body(other)).raw) == []
        # a non-event envelope
        non_event = {"type": "url_verification", "challenge": "c"}
        assert await ch.normalize(ch.handle_webhook({}, _body(non_event)).raw or _empty_raw()) == []

    asyncio.run(scenario())


def _empty_raw():
    from tabvis.channels.core.contract import RawInbound

    return RawInbound(external_event_id="", external_conversation_id="", external_account_ref="", payload={})


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        slack = _channel()
        gw.register_plugin(slack)
        gw.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
        await gw.start_plugin("slack")

        raw = slack.handle_webhook({}, _body(_event_callback("C_A", "run this"))).raw
        (result,) = await gw.receive_webhook("ca_slack", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        slack = _channel()
        gw.register_plugin(slack)
        gw.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
        await gw.start_plugin("slack")

        # Slack retries on non-2xx (X-Slack-Retry-Num) and fires message+app_mention — all share ts.
        body = _body(_event_callback("C_B", "hi", ts="1700000000.000200"))
        raw = slack.handle_webhook({}, body).raw
        (first,) = await gw.receive_webhook("ca_slack", raw)
        (retry,) = await gw.receive_webhook("ca_slack", raw)
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_channel_id_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        slack = _channel(fake)
        gw.register_plugin(slack)
        gw.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
        await gw.start_plugin("slack")

        # An inbound message creates the conversation<->channel binding the outbound needs.
        raw = slack.handle_webhook({}, _body(_event_callback("C_send", "hello"))).raw
        (inbound,) = await gw.receive_webhook("ca_slack", raw)

        receipt = await gw.deliver(
            "ca_slack",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "1700000000.000999"
        assert fake.calls == [("C_send", "done")]  # sent to the right channel with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        slack = _channel()
        gw.register_plugin(slack)
        gw.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
        await gw.start_plugin("slack")
        receipt = await gw.deliver(
            "ca_slack", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (static token + send over a mock transport) -----------------------------------


def test_client_sends_with_static_bearer_token() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/api/chat.postMessage"):
                # No token-exchange round trip: the xoxb- bot token is the bearer directly.
                assert request.headers["Authorization"] == "Bearer xoxb-test"
                body = json.loads(request.content)
                assert body == {"channel": "C1", "text": "hi there", "mrkdwn": True}
                return httpx.Response(200, json={"ok": True, "ts": "1700000000.000123"})
            return httpx.Response(404, json={"ok": False, "error": "unknown_method"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sc = SlackClient(_config(), client=client)
        message_ts = await sc.send_text("C1", "hi there")
        assert message_ts == "1700000000.000123"
        # Only the send endpoint is hit — there is no separate token endpoint for Slack.
        assert seen == ["/api/chat.postMessage"]
        await sc.aclose()

    asyncio.run(scenario())


def test_client_raises_on_logical_failure() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            # Slack returns HTTP 200 with ok:false on logical failure — must be treated as an error.
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})

        from tabvis.channels.plugins._platform.rest import ChannelApiError

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sc = SlackClient(_config(), client=client)
        with pytest.raises(ChannelApiError) as exc:
            await sc.send_text("C1", "hi")
        assert exc.value.code == "not_in_channel"
        await sc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        slack = _channel(fake)
        gw.register_plugin(slack)
        gw.register_account(ChannelAccount(channel_account_id="ca_slack", plugin_id="slack"))
        assert (await slack.health()).status == "stopped"
        await gw.start_plugin("slack")
        assert (await slack.health()).status == "ready"
        await gw.registry.stop("slack")
        assert (await slack.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
