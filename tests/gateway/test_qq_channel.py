"""QQ official-bot channel plugin — Ed25519 verify, op-13 handshake, normalize, delivery, e2e."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("cryptography")

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.qq import QQChannel, QQConfig
from tabvis.channels.plugins.qq import crypto
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

ACCOUNT = "ca_qq"
SECRET = "an-app-secret-value"


def _config() -> QQConfig:
    return QQConfig(app_id="app123", secret=SECRET, channel_account_id=ACCOUNT)


def _sign_event(timestamp: str, body: bytes) -> str:
    return crypto._private_key(SECRET).sign(str(timestamp).encode() + body).hex()


def _signed(payload: dict, *, ts: str = "1700000000") -> tuple[dict, bytes]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"X-Signature-Ed25519": _sign_event(ts, body), "X-Signature-Timestamp": ts}
    return headers, body


def _group_event(event_id: str, group: str, text: str, *, msg_id: str = "m1") -> dict:
    return {"op": 0, "id": event_id, "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": msg_id, "content": f" {text}", "group_openid": group,
                  "author": {"member_openid": "member_x"}}}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_group(self, group_openid, content, *, msg_id=None) -> str:
        self.calls.append(("group", group_openid, content, msg_id))
        return "sent-1"

    async def send_c2c(self, user_openid, content, *, msg_id=None) -> str:
        self.calls.append(("c2c", user_openid, content, msg_id))
        return "sent-2"

    async def send_channel(self, channel_id, content, *, msg_id=None) -> str:
        self.calls.append(("channel", channel_id, content, msg_id))
        return "sent-3"

    async def aclose(self) -> None:
        pass


def _channel(fake: _FakeClient | None = None) -> QQChannel:
    return QQChannel(_config(), client=fake if fake is not None else _FakeClient())


# --- crypto ------------------------------------------------------------------------------------


def test_event_signature_roundtrip() -> None:
    body = b'{"op":0}'
    assert crypto.verify_event(SECRET, "123", body, _sign_event("123", body))
    assert not crypto.verify_event(SECRET, "123", body, "00" * 64)  # wrong signature
    assert not crypto.verify_event(SECRET, "123", body, None)


# --- webhook: validation handshake + rejection -------------------------------------------------


def test_validation_handshake() -> None:
    headers, body = _signed({"op": 13, "d": {"plain_token": "pt", "event_ts": "1700000000"}})
    result = _channel().handle_webhook(headers, body)
    assert result.validation is not None
    assert result.validation["plain_token"] == "pt"
    assert result.validation["signature"] == crypto.sign_validation(SECRET, "1700000000", "pt")


def test_bad_signature_rejected() -> None:
    _, body = _signed({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})
    result = _channel().handle_webhook({"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": "1"}, body)
    assert result.rejected


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_group_and_c2c() -> None:
    async def scenario() -> None:
        ch = _channel()
        headers, body = _signed(_group_event("e1", "GID", "hello bot"))
        raw = ch.handle_webhook(headers, body).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"  # leading space stripped
        assert msg.external_conversation_id == "group:GID"
        assert ch._last_msg_id["group:GID"] == "m1"

        c2c = {"op": 0, "id": "e2", "t": "C2C_MESSAGE_CREATE",
               "d": {"id": "m2", "content": "hi", "author": {"user_openid": "user_y"}}}
        h2, b2 = _signed(c2c)
        (msg2,) = await ch.normalize(ch.handle_webhook(h2, b2).raw)
        assert msg2.external_conversation_id == "c2c:user_y"

    asyncio.run(scenario())


# --- end to end + passive-reply delivery -------------------------------------------------------


def test_e2e_creates_run_and_delivers_passive_reply() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        ch = _channel(fake)
        gw.register_plugin(ch)
        gw.register_account(ChannelAccount(channel_account_id=ACCOUNT, plugin_id="qq"))
        await gw.start_plugin("qq")

        headers, body = _signed(_group_event("e1", "GID", "question?", msg_id="trigger-77"))
        (inbound,) = await gw.receive_webhook(ACCOUNT, ch.handle_webhook(headers, body).raw)
        assert inbound.run_id and inbound.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=inbound.conversation_id)]
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

        receipt = await gw.deliver(
            ACCOUNT,
            OutboundMessage(delivery_id="d1", conversation_id=inbound.conversation_id, run_id=None, text="answer"),
        )
        assert receipt.status == "succeeded" and receipt.external_message_id == "sent-1"
        # delivered to the group, as a passive reply to the triggering message id.
        assert fake.calls == [("group", "GID", "answer", "trigger-77")]

    asyncio.run(scenario())
