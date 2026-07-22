"""WhatsApp Cloud (Meta Graph) channel plugin — crypto, webhook decoding, normalize, deliver, e2e.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus WhatsApp's own webhook
verification (the ``hub.*`` GET subscription handshake and the ``X-Hub-Signature-256`` HMAC over the
raw POST body).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.whatsapp import WhatsAppChannel, WhatsAppConfig
from tabvis.channels.plugins.whatsapp import crypto
from tabvis.channels.plugins.whatsapp.client import WhatsAppClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- helpers -----------------------------------------------------------------------------------


def _config(**kw) -> WhatsAppConfig:
    base = dict(
        phone_number_id="7794189252778687",
        access_token="perm-token",
        app_secret="app-secret",
        verify_token="vtok",
    )
    base.update(kw)
    return WhatsAppConfig(**base)


def _text_event(
    wamid: str,
    sender: str,
    text: str,
    *,
    business_number: str = "15551797781",
    profile_name: str = "Jessica",
) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "215589313241560883",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": business_number,
                                "phone_number_id": "7794189252778687",
                            },
                            "contacts": [{"profile": {"name": profile_name}, "wa_id": sender}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wamid,
                                    "timestamp": "1758254144",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _sign(secret: str, body: bytes) -> str:
    """The reference X-Hub-Signature-256 signer Meta uses: sha256=<hex HMAC-SHA256 of the raw body>."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _signed_headers(secret: str, body: bytes) -> dict:
    return {"X-Hub-Signature-256": _sign(secret, body)}


class _FakeClient:
    """Stands in for WhatsAppClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, to: str, text: str, *, reply_to=None, preview_url: bool = True) -> str:
        self.calls.append((to, text))
        return "wamid.sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> WhatsAppChannel:
    return WhatsAppChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_whatsapp_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "whatsapp"
    # WhatsApp verifies its own webhooks (custom scheme), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: signature -------------------------------------------------------------------------


def test_signature_roundtrip_and_rejection() -> None:
    secret = "app-secret"
    body = _body({"object": "whatsapp_business_account"})
    sig = crypto.whatsapp_signature(secret, body)
    assert sig.startswith("sha256=")
    assert sig == _sign(secret, body)  # exactly Meta's formula
    assert crypto.verify_signature(secret, body, sig)
    assert not crypto.verify_signature(secret, body, "sha256=deadbeef")
    assert not crypto.verify_signature(secret, body + b"x", sig)  # body tampered
    assert not crypto.verify_signature(secret, body, sig[len("sha256="):])  # missing sha256= prefix
    assert not crypto.verify_signature("", body, sig)  # missing secret
    assert not crypto.verify_signature(secret, body, None)


# --- webhook decoding: GET subscription handshake ----------------------------------------------


def test_handle_webhook_verify_handshake_echoes_challenge() -> None:
    ch = _channel()
    result = ch.handle_webhook(
        {},
        b"",
        params={"hub.mode": "subscribe", "hub.verify_token": "vtok", "hub.challenge": "ch-123"},
    )
    assert result.challenge == "ch-123"
    assert result.raw is None and not result.rejected


def test_handle_webhook_verify_rejects_bad_token_and_mode() -> None:
    ch = _channel()
    bad_token = ch.handle_webhook(
        {}, b"", params={"hub.mode": "subscribe", "hub.verify_token": "WRONG", "hub.challenge": "c"}
    )
    assert bad_token.rejected and bad_token.challenge is None

    bad_mode = ch.handle_webhook(
        {}, b"", params={"hub.mode": "unsubscribe", "hub.verify_token": "vtok", "hub.challenge": "c"}
    )
    assert bad_mode.rejected

    missing_challenge = ch.handle_webhook(
        {}, b"", params={"hub.mode": "subscribe", "hub.verify_token": "vtok"}
    )
    assert missing_challenge.rejected


def test_handle_webhook_verify_rejects_when_verify_token_unset() -> None:
    ch = _channel(verify_token="")
    result = ch.handle_webhook(
        {}, b"", params={"hub.mode": "subscribe", "hub.verify_token": "anything", "hub.challenge": "c"}
    )
    assert result.rejected  # unconfigured → 503, never accept the subscription


# --- webhook decoding: POST signature verification ---------------------------------------------


def test_handle_webhook_requires_valid_signature() -> None:
    ch = _channel()
    body = _body(_text_event("wamid.1", "13557825698", "hi"))

    ok = ch.handle_webhook(_signed_headers("app-secret", body), body)
    assert ok.raw is not None and not ok.rejected
    assert ok.raw.external_event_id == "wamid.1"
    assert ok.raw.external_conversation_id == "13557825698"

    bad = ch.handle_webhook({"X-Hub-Signature-256": "sha256=nope"}, body)
    assert bad.rejected

    missing = ch.handle_webhook({}, body)  # no signature header at all
    assert missing.rejected


def test_handle_webhook_rejects_when_app_secret_unset() -> None:
    ch = _channel(app_secret="")
    body = _body(_text_event("wamid.1", "13557825698", "hi"))
    result = ch.handle_webhook(_signed_headers("app-secret", body), body)
    assert result.rejected  # unconfigured → 503, refuse inbound


def test_handle_webhook_rejects_invalid_json_and_wrong_object() -> None:
    ch = _channel()
    result = ch.handle_webhook(_signed_headers("app-secret", b"not json"), b"not json")
    assert result.rejected

    other = _body({"object": "page", "entry": []})  # a Messenger event, not WhatsApp
    result = ch.handle_webhook(_signed_headers("app-secret", other), other)
    assert result.rejected


# --- normalize ---------------------------------------------------------------------------------


def _decode(ch: WhatsAppChannel, payload: dict):
    body = _body(payload)
    return ch.handle_webhook(_signed_headers("app-secret", body), body).raw


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        raw = _decode(ch, _text_event("wamid.abc", "13557825698", "hello bot"))
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "13557825698"  # DM: chat_id == sender wa_id
        assert msg.external_event_id == "wamid.abc"  # the wamid is the dedupe key
        assert msg.external_user_id == "13557825698"

    asyncio.run(scenario())


def test_normalize_media_message_becomes_placeholder() -> None:
    async def scenario() -> None:
        ch = _channel()
        event = _text_event("wamid.img", "13557825698", "")
        message = event["entry"][0]["changes"][0]["value"]["messages"][0]
        message["type"] = "image"
        del message["text"]
        message["image"] = {"id": "media-1", "caption": "a photo"}
        (msg,) = await ch.normalize(_decode(ch, event))
        assert msg.text == "[image: a photo]"

    asyncio.run(scenario())


def test_normalize_ignores_own_number_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # A message whose sender is our own business number — a defensive self-echo guard.
        own = _text_event("wamid.self", "15551797781", "loop?", business_number="15551797781")
        assert await ch.normalize(_decode(ch, own)) == []

        # A delivery-status callback carries `statuses`, not `messages` — never dispatched.
        status_payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"display_phone_number": "15551797781", "phone_number_id": "x"},
                                "statuses": [{"id": "wamid.out", "status": "delivered"}],
                            },
                        }
                    ],
                }
            ],
        }
        assert await ch.normalize(_decode(ch, status_payload)) == []

        # A non-"messages" change field (e.g. a template status update) produces nothing.
        other = {
            "object": "whatsapp_business_account",
            "entry": [{"id": "1", "changes": [{"field": "message_template_status_update", "value": {}}]}],
        }
        assert await ch.normalize(_decode(ch, other)) == []

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wa = _channel()
        gw.register_plugin(wa)
        gw.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
        await gw.start_plugin("whatsapp")

        raw = _decode(wa, _text_event("wamid.evt1", "13557825698", "run this"))
        (result,) = await gw.receive_webhook("ca_wa", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wa = _channel()
        gw.register_plugin(wa)
        gw.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
        await gw.start_plugin("whatsapp")

        raw = _decode(wa, _text_event("wamid.dup", "13557825698", "hi"))
        (first,) = await gw.receive_webhook("ca_wa", raw)
        (retry,) = await gw.receive_webhook("ca_wa", raw)  # Meta re-delivers the same wamid (up to 7 days)
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e
            for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_recipient_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        wa = _channel(fake)
        gw.register_plugin(wa)
        gw.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
        await gw.start_plugin("whatsapp")

        # An inbound message creates the conversation<->wa_id binding the outbound needs.
        raw = _decode(wa, _text_event("wamid.send", "13557825698", "hello"))
        (inbound,) = await gw.receive_webhook("ca_wa", raw)

        receipt = await gw.deliver(
            "ca_wa",
            OutboundMessage(
                delivery_id="dlv-1",
                conversation_id=inbound.conversation_id,
                run_id=inbound.run_id,
                text="done",
            ),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "wamid.sent"
        assert fake.calls == [("13557825698", "done")]  # sent to the right wa_id with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        wa = _channel()
        gw.register_plugin(wa)
        gw.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
        await gw.start_plugin("whatsapp")
        receipt = await gw.deliver(
            "ca_wa",
            OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi"),
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (permanent token + send over a mock transport) --------------------------------


def test_client_sends_with_bearer_token() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            assert request.url.path == "/v20.0/7794189252778687/messages"
            assert request.headers["Authorization"] == "Bearer perm-token"
            body = json.loads(request.content)
            assert body["messaging_product"] == "whatsapp"
            assert body["to"] == "13557825698"
            assert body["type"] == "text"
            assert body["text"]["body"] == "hi there"
            # Graph success shape.
            return httpx.Response(200, json={"messages": [{"id": "wamid.out1"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wc = WhatsAppClient(_config(), client=client)
        message_id = await wc.send_text("13557825698", "hi there")
        assert message_id == "wamid.out1"
        assert seen == ["/v20.0/7794189252778687/messages"]  # one call, no separate token exchange
        await wc.aclose()

    asyncio.run(scenario())


def test_client_raises_on_graph_error() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"error": {"message": "bad recipient", "type": "OAuthException", "code": 131030}}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        wc = WhatsAppClient(_config(), client=client)
        try:
            await wc.send_text("13557825698", "hi")
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = True
            assert "graph error 131030" in str(exc)
        assert raised
        await wc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        wa = _channel(fake)
        gw.register_plugin(wa)
        gw.register_account(ChannelAccount(channel_account_id="ca_wa", plugin_id="whatsapp"))
        assert (await wa.health()).status == "stopped"
        await gw.start_plugin("whatsapp")
        assert (await wa.health()).status == "ready"
        await gw.registry.stop("whatsapp")
        assert (await wa.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
