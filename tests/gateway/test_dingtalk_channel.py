"""钉钉 DingTalk IM channel plugin — crypto, webhook decoding, normalize, deliver, and end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus DingTalk's own outgoing-robot
callback verification (the ``timestamp`` + ``sign`` HMAC-SHA256/base64 header pair, and its replay
window). DingTalk's HTTP callback has no url_verification challenge, so there is no challenge test.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.dingtalk import DingTalkChannel, DingTalkConfig
from tabvis.channels.plugins.dingtalk import crypto
from tabvis.channels.plugins.dingtalk.client import DingTalkClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> DingTalkConfig:
    base = dict(client_id="ding_test", client_secret="secret_test")
    base.update(kw)
    return DingTalkConfig(**base)


def _text_payload(
    msg_id: str,
    conversation_id: str,
    text: str,
    *,
    sender_staff_id: str = "staff_1234",
    chatbot_user_id: str = "bot_self",
    conversation_type: str = "2",
) -> dict:
    return {
        "msgtype": "text",
        "text": {"content": text},
        "msgId": msg_id,
        "conversationId": conversation_id,
        "conversationType": conversation_type,
        "senderId": "user-opaque-77",
        "senderStaffId": sender_staff_id,
        "senderNick": "Alice",
        "chatbotUserId": chatbot_user_id,
        "isInAtList": False,
        "atUsers": [{"dingtalkId": "$:LWCP_v1:..."}],
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=abc",
        "sessionWebhookExpiredTime": 9999999999999,
        "createAt": 1690000000000,
        "robotCode": "ding_robot_code",
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _signed_headers(secret: str = "secret_test", *, timestamp: str | None = None) -> dict[str, str]:
    """DingTalk signs ``timestamp+secret`` (not the body), so the headers are body-independent."""
    ts = timestamp if timestamp is not None else str(int(time.time() * 1000))
    return {"timestamp": ts, "sign": crypto.dingtalk_sign(ts, secret)}


class _FakeClient:
    """Stands in for DingTalkClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, conversation_id: str, text: str) -> str:
        self.calls.append((conversation_id, text))
        return "proc_sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> DingTalkChannel:
    return DingTalkChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_dingtalk_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "dingtalk"
    # DingTalk verifies its own webhooks (custom scheme), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_roundtrip_and_rejection() -> None:
    secret = "app-secret"
    ts = "1700000000000"
    sig = crypto.dingtalk_sign(ts, secret)
    # tolerance disabled so the fixed (old) timestamp doesn't trip the freshness window.
    assert crypto.verify_signature(secret, ts, sig, tolerance_ms=None)
    assert not crypto.verify_signature(secret, ts, "deadbeef", tolerance_ms=None)
    assert not crypto.verify_signature(secret, "1700000000001", sig, tolerance_ms=None)  # wrong ts
    assert not crypto.verify_signature(secret, "", sig, tolerance_ms=None)   # missing timestamp
    assert not crypto.verify_signature(secret, ts, None, tolerance_ms=None)  # missing signature
    assert not crypto.verify_signature("", ts, sig, tolerance_ms=None)       # missing secret


def test_signature_rejects_stale_timestamp() -> None:
    secret = "app-secret"
    stale = str(int((time.time() - 7200) * 1000))  # 2h old — outside the ~1h replay window
    sig = crypto.dingtalk_sign(stale, secret)
    assert not crypto.verify_signature(secret, stale, sig)  # default tolerance enforces freshness
    assert crypto.verify_signature(secret, stale, sig, tolerance_ms=None)  # window off -> sig alone ok


# --- webhook decoding: signature --------------------------------------------------------------


def test_handle_webhook_accepts_valid_signature() -> None:
    ch = _channel()
    body = _body(_text_payload("msg-1", "cid_1", "hi"))
    result = ch.handle_webhook(_signed_headers(), body)
    assert result.raw is not None and not result.rejected
    assert result.raw.external_event_id == "msg-1"
    assert result.raw.external_conversation_id == "cid_1"
    assert result.challenge is None  # DingTalk has no challenge handshake


def test_handle_webhook_rejects_bad_signature() -> None:
    ch = _channel()
    body = _body(_text_payload("msg-2", "cid_1", "hi"))
    result = ch.handle_webhook({"timestamp": str(int(time.time() * 1000)), "sign": "nope"}, body)
    assert result.rejected and result.raw is None


def test_handle_webhook_rejects_missing_signature_headers() -> None:
    result = _channel().handle_webhook({}, _body(_text_payload("m", "c", "hi")))
    assert result.rejected


def test_handle_webhook_rejects_stale_timestamp() -> None:
    ch = _channel()
    stale = str(int((time.time() - 7200) * 1000))
    body = _body(_text_payload("msg-3", "cid_1", "hi"))
    result = ch.handle_webhook(_signed_headers(timestamp=stale), body)
    assert result.rejected  # replay guard


def test_handle_webhook_rejects_invalid_json() -> None:
    # A valid signature (over timestamp+secret) still can't rescue a non-JSON body.
    result = _channel().handle_webhook(_signed_headers(), b"not json")
    assert result.rejected


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        raw = ch.handle_webhook(_signed_headers(), _body(_text_payload("msg-1", "cid_1", "hello bot"))).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "cid_1"
        assert msg.external_event_id == "msg-1"
        assert msg.external_user_id == "staff_1234"

    asyncio.run(scenario())


def test_normalize_keeps_at_handles_in_text() -> None:
    async def scenario() -> None:
        ch = _channel()
        # @-mentions are structural (isInAtList) — the handle text must survive untouched.
        payload = _text_payload("msg-at", "cid_1", "@bot ssh user@host do it")
        payload["isInAtList"] = True
        (msg,) = await ch.normalize(ch.handle_webhook(_signed_headers(), _body(payload)).raw)
        assert msg.text == "@bot ssh user@host do it"

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # the bot's own message: sender staff id equals the receiving robot's chatbotUserId
        bot_msg = _text_payload("msg-bot", "cid_1", "loop?", sender_staff_id="bot_self", chatbot_user_id="bot_self")
        assert await ch.normalize(ch.handle_webhook(_signed_headers(), _body(bot_msg)).raw) == []
        # a non-message push (no msgtype) — e.g. a membership/control callback
        other = {"conversationId": "cid_1", "eventType": "chat_add_member"}
        assert await ch.normalize(ch.handle_webhook(_signed_headers(), _body(other)).raw) == []

    asyncio.run(scenario())


def test_normalize_rich_text() -> None:
    async def scenario() -> None:
        ch = _channel()
        payload = _text_payload("msg-rt", "cid_1", "")
        payload["msgtype"] = "richText"
        payload.pop("text")
        payload["content"] = {"richText": [{"text": "line one "}, {"text": "and two"}]}
        (msg,) = await ch.normalize(ch.handle_webhook(_signed_headers(), _body(payload)).raw)
        assert msg.text == "line one and two"

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        dingtalk = _channel()
        gw.register_plugin(dingtalk)
        gw.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
        await gw.start_plugin("dingtalk")

        raw = dingtalk.handle_webhook(_signed_headers(), _body(_text_payload("evt-1", "cid_A", "run this"))).raw
        (result,) = await gw.receive_webhook("ca_dingtalk", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        dingtalk = _channel()
        gw.register_plugin(dingtalk)
        gw.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
        await gw.start_plugin("dingtalk")

        raw = dingtalk.handle_webhook(_signed_headers(), _body(_text_payload("evt-dup", "cid_B", "hi"))).raw
        (first,) = await gw.receive_webhook("ca_dingtalk", raw)
        (retry,) = await gw.receive_webhook("ca_dingtalk", raw)  # DingTalk re-delivers the same msgId
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_conversation_id_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        dingtalk = _channel(fake)
        gw.register_plugin(dingtalk)
        gw.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
        await gw.start_plugin("dingtalk")

        # An inbound message creates the conversation<->conversationId binding the outbound needs.
        raw = dingtalk.handle_webhook(_signed_headers(), _body(_text_payload("evt-1", "cid_send", "hello"))).raw
        (inbound,) = await gw.receive_webhook("ca_dingtalk", raw)

        receipt = await gw.deliver(
            "ca_dingtalk",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "proc_sent"
        assert fake.calls == [("cid_send", "done")]  # sent to the right conversation with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        dingtalk = _channel()
        gw.register_plugin(dingtalk)
        gw.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
        await gw.start_plugin("dingtalk")
        receipt = await gw.deliver(
            "ca_dingtalk", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (token + send over a mock transport) ------------------------------------------


def test_client_fetches_token_then_sends() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/v1.0/oauth2/accessToken"):
                assert "Authorization" not in request.headers
                body = json.loads(request.content)
                assert body == {"appKey": "ding_test", "appSecret": "secret_test"}
                return httpx.Response(200, json={"accessToken": "atk-abc", "expireIn": 7200})
            if request.url.path.endswith("/v1.0/robot/groupMessages/send"):
                # DingTalk v1.0 takes the token as x-acs-dingtalk-access-token, not a bearer.
                assert request.headers["x-acs-dingtalk-access-token"] == "atk-abc"
                body = json.loads(request.content)
                assert body["openConversationId"] == "cid_1"
                assert body["msgKey"] == "sampleText"
                assert json.loads(body["msgParam"]) == {"content": "hi there"}
                return httpx.Response(200, json={"processQueryKey": "proc_1"})
            return httpx.Response(404, json={"code": "NotFound", "message": "not found"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dc = DingTalkClient(_config(), client=client)
        process_key = await dc.send_text("cid_1", "hi there")
        assert process_key == "proc_1"
        assert any(p.endswith("/v1.0/oauth2/accessToken") for p in seen)
        await dc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        dingtalk = _channel(fake)
        gw.register_plugin(dingtalk)
        gw.register_account(ChannelAccount(channel_account_id="ca_dingtalk", plugin_id="dingtalk"))
        assert (await dingtalk.health()).status == "stopped"
        await gw.start_plugin("dingtalk")
        assert (await dingtalk.health()).status == "ready"
        await gw.registry.stop("dingtalk")
        assert (await dingtalk.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
