"""企业微信 / WeCom callback channel plugin — crypto, webhook decoding, normalize, deliver, end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus WeCom's own callback
verification (SHA1 ``msg_signature``, the GET ``echostr`` URL-verification handshake, and the
AES-256-CBC ``WXBizMsgCrypt`` envelope).
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.wecom import WeComChannel, WeComConfig
from tabvis.channels.plugins.wecom import crypto
from tabvis.channels.plugins.wecom.client import WeComClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

# A valid 43-char EncodingAESKey: base64 of 32 deterministic bytes, minus its trailing "=" pad.
_AES_KEY = base64.b64encode(bytes(range(32))).decode("ascii")[:43]
_TOKEN = "TESTTOKEN"
_CORP = "ww_corp"


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> WeComConfig:
    base = dict(
        corp_id=_CORP,
        corp_secret="sekret",
        agent_id="1000002",
        token=_TOKEN,
        encoding_aes_key=_AES_KEY,
    )
    base.update(kw)
    return WeComConfig(**base)


def _text_xml(*, to=_CORP, frm="zhangsan", create_time="1710000000", content="你好", msg_id="123456789") -> str:
    return (
        "<xml>"
        f"<ToUserName>{to}</ToUserName>"
        f"<FromUserName>{frm}</FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        "<MsgType>text</MsgType>"
        f"<Content>{content}</Content>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )


def _event_xml(*, to=_CORP, frm="zhangsan", event="subscribe", create_time="1710000000") -> str:
    return (
        "<xml>"
        f"<ToUserName>{to}</ToUserName>"
        f"<FromUserName>{frm}</FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        "<MsgType>event</MsgType>"
        f"<Event>{event}</Event>"
        "</xml>"
    )


def _image_xml(*, to=_CORP, frm="zhangsan", create_time="1710000000", msg_id="777") -> str:
    return (
        "<xml>"
        f"<ToUserName>{to}</ToUserName>"
        f"<FromUserName>{frm}</FromUserName>"
        f"<CreateTime>{create_time}</CreateTime>"
        "<MsgType>image</MsgType>"
        "<PicUrl>http://example/pic</PicUrl>"
        f"<MsgId>{msg_id}</MsgId>"
        "</xml>"
    )


def _wecom_encrypt(encoding_aes_key: str, receive_id: str, plaintext: str) -> str:
    """Mirror image of crypto.decrypt_message: random16 | htonl(len) | plaintext | receive_id, PKCS7(32)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = base64.b64decode(encoding_aes_key + "=")
    iv = key[:16]
    msg = plaintext.encode("utf-8")
    # A fixed 16-byte prefix keeps the fixture deterministic; only the first 16 bytes are dropped.
    content = b"0123456789ABCDEF" + struct.pack(">I", len(msg)) + msg + receive_id.encode("utf-8")
    pad = 32 - (len(content) % 32)
    content += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(content) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def _post(ch: WeComChannel, xml: str, *, receive_id=_CORP, ts="1710000000", nonce="nonce1", token=_TOKEN):
    encrypt = _wecom_encrypt(_AES_KEY, receive_id, xml)
    body = f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode("utf-8")
    sig = crypto.wecom_signature(token, ts, nonce, encrypt)
    query = {"msg_signature": sig, "timestamp": ts, "nonce": nonce}
    return ch.handle_webhook(query, body)


class _FakeClient:
    """Stands in for WeComClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, user_id: str, text: str) -> str:
        self.calls.append((user_id, text))
        return "MSG_SENT"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> WeComChannel:
    return WeComChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_wecom_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "wecom"
    # WeCom verifies its own webhooks (custom SHA1 scheme), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_is_sorted_sha1_and_rejects_mismatch() -> None:
    sig = crypto.wecom_signature(_TOKEN, "1710000000", "nonce1", "ENC")
    assert crypto.verify_signature(_TOKEN, "1710000000", "nonce1", "ENC", sig)
    # Order of the four inputs must not matter to the digest — they are sorted before hashing.
    assert crypto.wecom_signature("b", "a", "d", "c") == crypto.wecom_signature("a", "b", "c", "d")
    assert not crypto.verify_signature(_TOKEN, "1710000000", "nonce1", "ENC", "deadbeef")
    assert not crypto.verify_signature(_TOKEN, "1710000000", "other-nonce", "ENC", sig)
    assert not crypto.verify_signature(_TOKEN, "", "nonce1", "ENC", sig)      # missing timestamp
    assert not crypto.verify_signature(_TOKEN, "1710000000", "nonce1", "ENC", None)


# --- crypto: AES decrypt (needs cryptography) --------------------------------------------------


def test_decrypt_message_roundtrip_and_receive_id_enforced() -> None:
    pytest.importorskip("cryptography")
    xml = _text_xml(content="世界")
    encrypt = _wecom_encrypt(_AES_KEY, _CORP, xml)
    assert crypto.decrypt_message(_AES_KEY, _CORP, encrypt) == xml
    # A receive_id that isn't our corp id is a hard failure (suite/provider confusion, tampering).
    with pytest.raises(Exception):
        crypto.decrypt_message(_AES_KEY, "ww_other_corp", encrypt)


def test_decrypt_rejects_bad_aes_key_length() -> None:
    with pytest.raises(ValueError):
        crypto.decrypt_message("too-short", _CORP, "AAAA")


# --- webhook decoding: url-verification / signature / decrypt ----------------------------------


def test_handle_webhook_url_verification_returns_decrypted_echostr() -> None:
    pytest.importorskip("cryptography")
    ch = _channel()
    echostr = _wecom_encrypt(_AES_KEY, _CORP, "echo-plain-1234")
    ts, nonce = "1710000000", "nonce1"
    sig = crypto.wecom_signature(_TOKEN, ts, nonce, echostr)
    result = ch.handle_webhook(
        {"msg_signature": sig, "timestamp": ts, "nonce": nonce, "echostr": echostr}, b""
    )
    assert result.challenge == "echo-plain-1234"  # the DECRYPTED plaintext, not the ciphertext
    assert result.raw is None and not result.rejected


def test_handle_webhook_url_verification_rejects_bad_signature() -> None:
    ch = _channel()
    echostr = _wecom_encrypt(_AES_KEY, _CORP, "echo")
    result = ch.handle_webhook(
        {"msg_signature": "nope", "timestamp": "1710000000", "nonce": "n", "echostr": echostr}, b""
    )
    assert result.rejected and result.challenge is None


def test_handle_webhook_valid_post_yields_raw() -> None:
    pytest.importorskip("cryptography")
    ch = _channel()
    result = _post(ch, _text_xml())
    assert result.raw is not None and not result.rejected
    assert result.raw.external_event_id == "123456789"
    assert result.raw.external_conversation_id == "ww_corp:zhangsan"
    assert result.raw.external_account_ref == "ww_corp"


def test_handle_webhook_rejects_tampered_signature() -> None:
    pytest.importorskip("cryptography")
    ch = _channel()
    encrypt = _wecom_encrypt(_AES_KEY, _CORP, _text_xml())
    body = f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>".encode("utf-8")
    bad = ch.handle_webhook(
        {"msg_signature": "deadbeef", "timestamp": "1710000000", "nonce": "n1"}, body
    )
    assert bad.rejected and bad.reason == "signature mismatch"


def test_handle_webhook_rejects_invalid_xml_and_oversized_body() -> None:
    ch = _channel()
    assert ch.handle_webhook({"timestamp": "1", "nonce": "n"}, b"not xml").rejected
    huge = b"<xml><Encrypt>" + b"A" * 70000 + b"</Encrypt></xml>"
    assert ch.handle_webhook({"timestamp": "1", "nonce": "n"}, huge).rejected


def test_handle_webhook_fallback_event_id_when_msgid_absent() -> None:
    pytest.importorskip("cryptography")
    ch = _channel()
    xml = (
        "<xml>"
        f"<ToUserName>{_CORP}</ToUserName>"
        "<FromUserName>lisi</FromUserName>"
        "<CreateTime>1710000042</CreateTime>"
        "<MsgType>text</MsgType>"
        "<Content>hi</Content>"
        "</xml>"
    )
    raw = _post(ch, xml).raw
    assert raw is not None
    assert raw.external_event_id == "lisi:1710000042"  # MsgId absent -> FromUserName:CreateTime


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        raw = _post(ch, _text_xml(content="run this")).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "run this"
        assert msg.external_conversation_id == "ww_corp:zhangsan"
        assert msg.external_event_id == "123456789"
        assert msg.external_user_id == "zhangsan"

    asyncio.run(scenario())


def test_normalize_ignores_lifecycle_events_and_non_text_types() -> None:
    async def scenario() -> None:
        ch = _channel()
        # Lifecycle event (subscribe) — WeCom's analog of a bot/system message: dropped.
        assert await ch.normalize(_post(ch, _event_xml(event="subscribe")).raw) == []
        assert await ch.normalize(_post(ch, _event_xml(event="enter_agent")).raw) == []
        # A non-text, non-event message type (image) produces no inbound text.
        assert await ch.normalize(_post(ch, _image_xml()).raw) == []

    asyncio.run(scenario())


def test_normalize_bare_event_becomes_start_command() -> None:
    async def scenario() -> None:
        ch = _channel()
        # A non-lifecycle event with no Content is coerced to "/start" (e.g. a menu click).
        (msg,) = await ch.normalize(_post(ch, _event_xml(event="click")).raw)
        assert msg.text == "/start"

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wecom = _channel()
        gw.register_plugin(wecom)
        gw.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
        await gw.start_plugin("wecom")

        raw = _post(wecom, _text_xml(msg_id="evt-1", content="run this")).raw
        (result,) = await gw.receive_webhook("ca_wecom", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wecom = _channel()
        gw.register_plugin(wecom)
        gw.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
        await gw.start_plugin("wecom")

        # WeCom re-delivers the same MsgId when an ACK is slow — dedup must return the original.
        raw = _post(wecom, _text_xml(msg_id="dup-1", content="hi")).raw
        (first,) = await gw.receive_webhook("ca_wecom", raw)
        raw2 = _post(wecom, _text_xml(msg_id="dup-1", content="hi")).raw
        (retry,) = await gw.receive_webhook("ca_wecom", raw2)
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
        wecom = _channel(fake)
        gw.register_plugin(wecom)
        gw.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
        await gw.start_plugin("wecom")

        # An inbound message creates the conversation<->chat binding the outbound needs.
        raw = _post(wecom, _text_xml(msg_id="evt-send", content="hello")).raw
        (inbound,) = await gw.receive_webhook("ca_wecom", raw)

        receipt = await gw.deliver(
            "ca_wecom",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "MSG_SENT"
        # touser is the userid part of the synthesized "corp:user" chat id.
        assert fake.calls == [("zhangsan", "done")]

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wecom = _channel()
        gw.register_plugin(wecom)
        gw.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
        await gw.start_plugin("wecom")
        receipt = await gw.deliver(
            "ca_wecom", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (token + send over a mock transport) ------------------------------------------


def test_client_fetches_token_then_sends() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/cgi-bin/gettoken"):
                assert request.url.params["corpid"] == _CORP
                assert request.url.params["corpsecret"] == "sekret"
                assert "Authorization" not in request.headers
                return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "access_token": "tok-abc", "expires_in": 7200})
            if request.url.path.endswith("/cgi-bin/message/send"):
                assert request.url.params["access_token"] == "tok-abc"  # token rides in the query
                body = json.loads(request.content)
                assert body["touser"] == "zhangsan"
                assert body["agentid"] == 1000002  # coerced to int
                assert body["text"]["content"] == "hi there"
                return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "msgid": "MSGID-1"})
            return httpx.Response(404, json={"errcode": 404, "errmsg": "not found"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wc = WeComClient(_config(), client=client)
        message_id = await wc.send_text("zhangsan", "hi there")
        assert message_id == "MSGID-1"
        assert any(p.endswith("/cgi-bin/gettoken") for p in seen)
        await wc.aclose()

    asyncio.run(scenario())


def test_client_refetches_token_once_on_rejection() -> None:
    async def scenario() -> None:
        tokens = iter(["stale-tok", "fresh-tok"])
        sends: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cgi-bin/gettoken"):
                return httpx.Response(200, json={"errcode": 0, "access_token": next(tokens), "expires_in": 7200})
            # First send with the stale token is rejected (42001); the retry with the fresh one wins.
            token = request.url.params["access_token"]
            sends.append(token)
            if token == "stale-tok":
                return httpx.Response(200, json={"errcode": 42001, "errmsg": "access_token expired"})
            return httpx.Response(200, json={"errcode": 0, "msgid": "OK-2"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wc = WeComClient(_config(), client=client)
        message_id = await wc.send_text("zhangsan", "hi")
        assert message_id == "OK-2"
        assert sends == ["stale-tok", "fresh-tok"]  # evicted the rejected token and refetched once
        await wc.aclose()

    asyncio.run(scenario())


def test_client_raises_on_non_token_error() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/cgi-bin/gettoken"):
                return httpx.Response(200, json={"errcode": 0, "access_token": "t", "expires_in": 7200})
            return httpx.Response(200, json={"errcode": 60020, "errmsg": "not allow to access from your ip"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wc = WeComClient(_config(), client=client)
        with pytest.raises(Exception):  # a non-token errcode fails immediately, no retry
            await wc.send_text("zhangsan", "hi")
        await wc.aclose()

    asyncio.run(scenario())


# --- config ------------------------------------------------------------------------------------


def test_config_from_env_reads_tabvis_wecom_vars() -> None:
    env = {
        "TABVIS_WECOM_CORP_ID": "ww_env",
        "TABVIS_WECOM_CORP_SECRET": "s",
        "TABVIS_WECOM_AGENT_ID": "1000002",
        "TABVIS_WECOM_TOKEN": "tok",
        "TABVIS_WECOM_ENCODING_AES_KEY": _AES_KEY,
    }
    cfg = WeComConfig.from_env(env)
    assert cfg.corp_id == "ww_env" and cfg.base_url == "https://qyapi.weixin.qq.com"
    with pytest.raises(RuntimeError):
        WeComConfig.from_env({"TABVIS_WECOM_CORP_ID": "only-corp"})  # missing the rest


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        wecom = _channel(fake)
        gw.register_plugin(wecom)
        gw.register_account(ChannelAccount(channel_account_id="ca_wecom", plugin_id="wecom"))
        assert (await wecom.health()).status == "stopped"
        await gw.start_plugin("wecom")
        assert (await wecom.health()).status == "ready"
        await gw.registry.stop("wecom")
        assert (await wecom.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
