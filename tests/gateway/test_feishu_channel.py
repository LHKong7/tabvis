"""Feishu / Lark IM channel plugin — crypto, webhook decoding, normalize, deliver, and end-to-end.

Mirrors ``test_channels.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus Feishu's own webhook
verification (url_verification challenge, verification token, v2 signature, AES-encrypted envelope).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.feishu import FeishuChannel, FeishuConfig
from tabvis.channels.plugins.feishu import crypto
from tabvis.channels.plugins.feishu.client import FeishuClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> FeishuConfig:
    base = dict(app_id="cli_test", app_secret="secret_test")
    base.update(kw)
    return FeishuConfig(**base)


def _text_event(event_id: str, chat_id: str, text: str, *, user_open_id: str = "ou_user") -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": "vtok",
            "app_id": "cli_test",
            "tenant_key": "tk",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": user_open_id}},
            "message": {
                "message_id": f"om_{event_id}",
                "chat_id": chat_id,
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": text}),
            },
        },
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class _FakeClient:
    """Stands in for FeishuClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, receive_id: str, text: str, *, receive_id_type: str = "chat_id") -> str:
        self.calls.append((receive_id, text))
        return "om_sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> FeishuChannel:
    return FeishuChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_feishu_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "feishu"
    # Feishu verifies its own webhooks (custom scheme), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_roundtrip_and_rejection() -> None:
    key = "enc-key"
    body = _body({"header": {"event_type": "im.message.receive_v1"}})
    sig = crypto.feishu_signature("1700000000", "nonce-1", key, body)
    assert crypto.verify_signature(key, "1700000000", "nonce-1", body, sig)
    assert not crypto.verify_signature(key, "1700000000", "nonce-1", body, "deadbeef")
    assert not crypto.verify_signature(key, "1700000000", "wrong-nonce", body, sig)
    assert not crypto.verify_signature(key, "", "nonce-1", body, sig)   # missing timestamp
    assert not crypto.verify_signature(key, "1700000000", "nonce-1", body, None)


# --- crypto: AES decrypt (needs cryptography) --------------------------------------------------


def _aes_encrypt(encrypt_key: str, plaintext: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    iv = os.urandom(16)
    data = plaintext.encode("utf-8")
    pad = 16 - (len(data) % 16)
    data += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def test_decrypt_envelope_roundtrip() -> None:
    pytest.importorskip("cryptography")
    key = "my-encrypt-key"
    plaintext = json.dumps({"hello": "世界", "n": 1})
    assert crypto.decrypt_envelope(key, _aes_encrypt(key, plaintext)) == plaintext


# --- webhook decoding: challenge / token / signature ------------------------------------------


def test_handle_webhook_url_verification_returns_challenge() -> None:
    ch = _channel(verification_token="vtok")
    body = _body({"type": "url_verification", "token": "vtok", "challenge": "ch-123"})
    result = ch.handle_webhook({}, body)
    assert result.challenge == "ch-123"
    assert result.raw is None and not result.rejected


def test_handle_webhook_rejects_bad_verification_token() -> None:
    ch = _channel(verification_token="vtok")
    body = _body({"type": "url_verification", "token": "WRONG", "challenge": "ch"})
    result = ch.handle_webhook({}, body)
    assert result.rejected and result.challenge is None


def test_handle_webhook_rejects_invalid_json() -> None:
    result = _channel().handle_webhook({}, b"not json")
    assert result.rejected


def test_handle_webhook_requires_valid_signature_when_encrypt_key_set() -> None:
    key = "enc-key"
    ch = _channel(encrypt_key=key)
    body = _body(_text_event("e1", "oc_1", "hi"))
    ts, nonce = "1700000000", "n1"
    good = crypto.feishu_signature(ts, nonce, key, body)

    ok = ch.handle_webhook(
        {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": good}, body
    )
    assert ok.raw is not None and not ok.rejected
    assert ok.raw.external_event_id == "e1" and ok.raw.external_conversation_id == "oc_1"

    bad = ch.handle_webhook(
        {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": "nope"}, body
    )
    assert bad.rejected

    missing = ch.handle_webhook({}, body)  # no signature headers at all
    assert missing.rejected


def test_handle_webhook_decrypts_encrypted_event() -> None:
    pytest.importorskip("cryptography")
    key = "enc-key"
    ch = _channel(encrypt_key=key)
    inner = _text_event("e-enc", "oc_enc", "secret hello")
    body = _body({"encrypt": _aes_encrypt(key, json.dumps(inner))})
    ts, nonce = "1700000000", "n1"
    sig = crypto.feishu_signature(ts, nonce, key, body)  # signature is over the raw (encrypted) body
    result = ch.handle_webhook(
        {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": sig}, body
    )
    assert result.raw is not None
    assert result.raw.payload["event"]["message"]["chat_id"] == "oc_enc"


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        raw = ch.handle_webhook({}, _body(_text_event("e1", "oc_1", "hello bot"))).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "oc_1"
        assert msg.external_event_id == "e1"
        assert msg.external_user_id == "ou_user"

    asyncio.run(scenario())


def test_normalize_cleans_mention_placeholders() -> None:
    async def scenario() -> None:
        ch = _channel()
        event = _text_event("e2", "oc_1", "@_user_1 do the thing")
        event["event"]["message"]["mentions"] = [{"key": "@_user_1", "name": "MyBot"}]
        (msg,) = await ch.normalize(ch.handle_webhook({}, _body(event)).raw)
        assert msg.text == "@MyBot do the thing"

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # a bot's own message
        bot_event = _text_event("e3", "oc_1", "loop?")
        bot_event["event"]["sender"]["sender_type"] = "app"
        assert await ch.normalize(ch.handle_webhook({}, _body(bot_event)).raw) == []
        # a non-message event type
        other = {"header": {"event_type": "im.chat.member.bot.added_v1"}, "event": {}}
        assert await ch.normalize(ch.handle_webhook({}, _body(other)).raw) == []

    asyncio.run(scenario())


def test_normalize_post_rich_text() -> None:
    async def scenario() -> None:
        ch = _channel()
        event = _text_event("e4", "oc_1", "")
        event["event"]["message"]["message_type"] = "post"
        event["event"]["message"]["content"] = json.dumps(
            {"title": "Report", "content": [[{"tag": "text", "text": "line one "}, {"tag": "a", "text": "link"}]]}
        )
        (msg,) = await ch.normalize(ch.handle_webhook({}, _body(event)).raw)
        assert "Report" in msg.text and "line one link" in msg.text

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        feishu = _channel()
        gw.register_plugin(feishu)
        gw.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
        await gw.start_plugin("feishu")

        raw = feishu.handle_webhook({}, _body(_text_event("evt-1", "oc_A", "run this"))).raw
        (result,) = await gw.receive_webhook("ca_feishu", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        feishu = _channel()
        gw.register_plugin(feishu)
        gw.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
        await gw.start_plugin("feishu")

        raw = feishu.handle_webhook({}, _body(_text_event("evt-dup", "oc_B", "hi"))).raw
        (first,) = await gw.receive_webhook("ca_feishu", raw)
        (retry,) = await gw.receive_webhook("ca_feishu", raw)  # Feishu re-delivers the same event
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_chat_id_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        feishu = _channel(fake)
        gw.register_plugin(feishu)
        gw.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
        await gw.start_plugin("feishu")

        # An inbound message creates the conversation<->chat binding the outbound needs.
        raw = feishu.handle_webhook({}, _body(_text_event("evt-1", "oc_send", "hello"))).raw
        (inbound,) = await gw.receive_webhook("ca_feishu", raw)

        receipt = await gw.deliver(
            "ca_feishu",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "om_sent"
        assert fake.calls == [("oc_send", "done")]  # sent to the right chat with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        feishu = _channel()
        gw.register_plugin(feishu)
        gw.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
        await gw.start_plugin("feishu")
        receipt = await gw.deliver(
            "ca_feishu", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (token + send over a mock transport) ------------------------------------------


def test_client_fetches_token_then_sends() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/tenant_access_token/internal"):
                assert "Authorization" not in request.headers
                return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-abc", "expire": 7200})
            if request.url.path.endswith("/im/v1/messages"):
                assert request.headers["Authorization"] == "Bearer t-abc"
                body = json.loads(request.content)
                assert json.loads(body["content"]) == {"text": "hi there"}
                return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"message_id": "om_1"}})
            return httpx.Response(404, json={"code": 1, "msg": "not found"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        fc = FeishuClient(_config(), client=client)
        message_id = await fc.send_text("oc_1", "hi there")
        assert message_id == "om_1"
        assert any(p.endswith("/tenant_access_token/internal") for p in seen)
        await fc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        feishu = _channel(fake)
        gw.register_plugin(feishu)
        gw.register_account(ChannelAccount(channel_account_id="ca_feishu", plugin_id="feishu"))
        assert (await feishu.health()).status == "stopped"
        await gw.start_plugin("feishu")
        assert (await feishu.health()).status == "ready"
        await gw.registry.stop("feishu")
        assert (await feishu.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
