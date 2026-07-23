"""LINE Messaging API channel plugin — crypto, webhook decoding, normalize, deliver, and end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus LINE's own webhook signature
verification (``base64(HMAC_SHA256(secret, raw_body))`` against ``X-Line-Signature``) and its
reply-token-then-push send routing.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.line import LineChannel, LineConfig
from tabvis.channels.plugins.line import crypto
from tabvis.channels.plugins.line.client import LineClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


SECRET = "line-channel-secret"


def _config(**kw) -> LineConfig:
    base = dict(channel_access_token="tok_test", channel_secret=SECRET)
    base.update(kw)
    return LineConfig(**base)


def _text_event(
    event_id: str,
    chat_id: str,
    text: str,
    *,
    reply_token: str = "rtok",
    user_id: str | None = None,
) -> dict:
    # In a LINE 1:1 DM the chat id *is* the sender's userId; ``user_id`` overrides only for self-echo.
    return {
        "type": "message",
        "webhookEventId": event_id,
        "replyToken": reply_token,
        "mode": "active",
        "timestamp": 1620000000000,
        "source": {"type": "user", "userId": user_id if user_id is not None else chat_id},
        "message": {"type": "text", "id": f"m_{event_id}", "text": text},
    }


def _webhook(*events: dict, destination: str = "Ubotdest") -> dict:
    return {"destination": destination, "events": list(events)}


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _signed(payload: dict) -> tuple[dict[str, str], bytes]:
    """Return ``(headers, raw_body)`` carrying a valid X-Line-Signature — LINE signs the raw bytes."""
    body = _body(payload)
    return {"X-Line-Signature": crypto.line_signature(SECRET, body)}, body


class _FakeClient:
    """Stands in for LineClient so deliver tests never touch the network."""

    def __init__(self, *, bot_user_id: str = "") -> None:
        self.replies: list[tuple[str, str]] = []
        self.pushes: list[tuple[str, str]] = []
        self.reply_fails = False
        self.bot_user_id = bot_user_id
        self.closed = False

    async def reply_text(self, reply_token: str, text: str) -> str:
        if self.reply_fails:
            raise RuntimeError("reply token already consumed")
        self.replies.append((reply_token, text))
        return "m_reply"

    async def push_text(self, to: str, text: str) -> str:
        self.pushes.append((to, text))
        return "m_push"

    async def get_bot_info(self) -> str:
        return self.bot_user_id

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> LineChannel:
    return LineChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_line_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "line"
    # LINE verifies its own webhooks (base64 HMAC, not the generic hex gate), so signed_webhooks is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_roundtrip_and_rejection() -> None:
    body = _body(_webhook(_text_event("e1", "Uchat", "hi")))
    sig = crypto.line_signature(SECRET, body)
    assert crypto.verify_line_signature(SECRET, body, sig)
    assert not crypto.verify_line_signature(SECRET, body, "deadbeef")
    assert not crypto.verify_line_signature("other-secret", body, sig)      # wrong key
    assert not crypto.verify_line_signature(SECRET, body + b"x", sig)       # body tampered
    assert not crypto.verify_line_signature(SECRET, body, None)             # missing signature
    assert not crypto.verify_line_signature("", body, sig)                  # missing secret


# --- webhook decoding: signature gate + the empty Verify ping ----------------------------------


def test_handle_webhook_accepts_valid_signature() -> None:
    ch = _channel()
    headers, body = _signed(_webhook(_text_event("e1", "Uchat", "hi")))
    result = ch.handle_webhook(headers, body)
    assert result.raw is not None and not result.rejected
    assert result.raw.external_event_id == "e1"
    assert result.raw.external_conversation_id == "Uchat"


def test_handle_webhook_rejects_bad_signature() -> None:
    ch = _channel()
    body = _body(_webhook(_text_event("e1", "Uchat", "hi")))
    result = ch.handle_webhook({"X-Line-Signature": "nope"}, body)
    assert result.rejected and result.raw is None
    # missing header entirely also fails closed
    assert ch.handle_webhook({}, body).rejected


def test_handle_webhook_rejects_invalid_json_after_valid_signature() -> None:
    ch = _channel()
    body = b"not json"
    sig = crypto.line_signature(SECRET, body)
    result = ch.handle_webhook({"X-Line-Signature": sig}, body)
    assert result.rejected


def test_handle_webhook_empty_verify_ping_is_a_signed_no_op() -> None:
    async def scenario() -> None:
        ch = _channel()
        # LINE's console "Verify" button posts an empty, correctly-signed events body.
        headers, body = _signed(_webhook())
        result = ch.handle_webhook(headers, body)
        assert result.raw is not None and not result.rejected  # verified -> 200
        assert await ch.normalize(result.raw) == []            # ...but nothing to ingest

    asyncio.run(scenario())


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        raw = ch.handle_webhook(*_signed(_webhook(_text_event("e1", "Uchat", "hello bot")))).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "Uchat"
        assert msg.external_event_id == "e1"
        assert msg.external_user_id == "Uchat"  # DM: sender userId == chat id

    asyncio.run(scenario())


def test_normalize_group_resolves_group_id_as_chat() -> None:
    async def scenario() -> None:
        ch = _channel()
        event = _text_event("e2", "ignored", "team ping")
        event["source"] = {"type": "group", "groupId": "Cgroup", "userId": "Umember"}
        raw = ch.handle_webhook(*_signed(_webhook(event))).raw
        (msg,) = await ch.normalize(raw)
        assert msg.external_conversation_id == "Cgroup"   # outbound target is the group id
        assert msg.external_user_id == "Umember"          # sender is still the user id

    asyncio.run(scenario())


def test_normalize_bundles_multiple_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        payload = _webhook(
            _text_event("ea", "Uchat", "first"),
            _text_event("eb", "Uchat", "second"),
        )
        msgs = await ch.normalize(ch.handle_webhook(*_signed(payload)).raw)
        assert [m.text for m in msgs] == ["first", "second"]
        assert [m.external_event_id for m in msgs] == ["ea", "eb"]

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        # a bot's own echoed message (source.userId == our bot id) is dropped
        ch = _channel(bot_user_id="Ubot")
        echo = _text_event("e3", "Uchat", "loop?", user_id="Ubot")
        assert await ch.normalize(ch.handle_webhook(*_signed(_webhook(echo))).raw) == []
        # a non-message event type produces no inbound message
        follow = {"type": "follow", "webhookEventId": "e4", "source": {"type": "user", "userId": "Uuser"}}
        assert await ch.normalize(ch.handle_webhook(*_signed(_webhook(follow))).raw) == []

    asyncio.run(scenario())


def test_normalize_media_surfaces_typed_placeholder() -> None:
    async def scenario() -> None:
        ch = _channel()
        event = _text_event("e5", "Uchat", "")
        event["message"] = {"type": "image", "id": "img_1"}
        (msg,) = await ch.normalize(ch.handle_webhook(*_signed(_webhook(event))).raw)
        assert msg.text == "[image]"

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        line = _channel()
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")

        raw = line.handle_webhook(*_signed(_webhook(_text_event("evt-1", "Uchat_A", "run this")))).raw
        (result,) = await gw.receive_webhook("ca_line", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        line = _channel()
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")

        raw = line.handle_webhook(*_signed(_webhook(_text_event("evt-dup", "Uchat_B", "hi")))).raw
        (first,) = await gw.receive_webhook("ca_line", raw)
        (retry,) = await gw.receive_webhook("ca_line", raw)  # LINE re-delivers the same webhookEventId
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_uses_reply_token_when_present() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        line = _channel(fake)
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")

        # An inbound message creates the conversation<->chat binding AND stashes the reply token.
        raw = line.handle_webhook(
            *_signed(_webhook(_text_event("evt-1", "Uchat_send", "hello", reply_token="RT-1")))
        ).raw
        (inbound,) = await gw.receive_webhook("ca_line", raw)

        receipt = await gw.deliver(
            "ca_line",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "m_reply"
        assert fake.replies == [("RT-1", "done")]  # free reply used, with the stashed token
        assert fake.pushes == []

    asyncio.run(scenario())


def test_deliver_pushes_when_no_reply_token() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        line = _channel(fake)
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")

        raw = line.handle_webhook(*_signed(_webhook(_text_event("evt-1", "Uchat_send", "hi")))).raw
        (inbound,) = await gw.receive_webhook("ca_line", raw)
        line._reply_tokens.clear()  # simulate the single-use token already spent / expired

        receipt = await gw.deliver(
            "ca_line",
            OutboundMessage(delivery_id="dlv-2", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="later"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "m_push"
        assert fake.pushes == [("Uchat_send", "later")]  # metered push fallback
        assert fake.replies == []

    asyncio.run(scenario())


def test_deliver_falls_back_to_push_when_reply_fails() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        fake.reply_fails = True  # e.g. the token was already consumed on LINE's side
        line = _channel(fake)
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")

        raw = line.handle_webhook(
            *_signed(_webhook(_text_event("evt-1", "Uchat_send", "hi", reply_token="RT-9")))
        ).raw
        (inbound,) = await gw.receive_webhook("ca_line", raw)

        receipt = await gw.deliver(
            "ca_line",
            OutboundMessage(delivery_id="dlv-3", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert fake.pushes == [("Uchat_send", "done")]  # reply raised -> fell through to push

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        line = _channel()
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        await gw.start_plugin("line")
        receipt = await gw.deliver(
            "ca_line", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (static bearer + reply/push/info over a mock transport) -----------------------


def test_client_sends_reply_push_and_reads_bot_info() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            # LINE has no token endpoint: the static channel token is the bearer on every call.
            assert request.headers["Authorization"] == "Bearer tok_test"
            if request.url.path == "/v2/bot/message/reply":
                body = json.loads(request.content)
                assert body["replyToken"] == "RT-1"
                assert body["messages"] == [{"type": "text", "text": "hi there"}]
                return httpx.Response(200, json={"sentMessages": [{"id": "m1"}]})
            if request.url.path == "/v2/bot/message/push":
                body = json.loads(request.content)
                assert body["to"] == "Uchat"
                assert body["messages"] == [{"type": "text", "text": "yo"}]
                return httpx.Response(200, json={})  # LINE also returns a bare {} on success
            if request.url.path == "/v2/bot/info":
                return httpx.Response(200, json={"userId": "Ubot"})
            return httpx.Response(404, json={"message": "not found"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        lc = LineClient(_config(), client=client)
        assert await lc.reply_text("RT-1", "hi there") == "m1"
        assert await lc.push_text("Uchat", "yo") == ""      # bare {} -> no sent-message id
        assert await lc.get_bot_info() == "Ubot"
        # no token-exchange round-trip happened
        assert not any("token" in p or "oauth" in p for p in seen)
        await lc.aclose()

    asyncio.run(scenario())


def test_client_raises_on_error_status() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"message": "Invalid reply token"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        lc = LineClient(_config(), client=client)
        with pytest.raises(Exception):
            await lc.reply_text("RT-dead", "hi")
        await lc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient(bot_user_id="Ubot")
        line = _channel(fake)
        gw.register_plugin(line)
        gw.register_account(ChannelAccount(channel_account_id="ca_line", plugin_id="line"))
        assert (await line.health()).status == "stopped"
        await gw.start_plugin("line")
        assert (await line.health()).status == "ready"
        assert line._bot_user_id == "Ubot"  # start fetched our own userId via /bot/info
        await gw.registry.stop("line")
        assert (await line.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
