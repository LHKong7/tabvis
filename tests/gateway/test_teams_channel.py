"""Microsoft Teams / Bot Framework channel plugin — JWT auth, normalize, deliver, and end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus Teams' own webhook
verification. Teams has no HMAC/challenge — inbound auth is a signed JWT Bearer token (RS256 via the
Bot Framework JWKS), so the tests mint real RS256 tokens with an in-test RSA keypair and load its
public JWK into the channel, then check the accepted and rejected paths.
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.teams import TeamsChannel, TeamsConfig
from tabvis.channels.plugins.teams import crypto
from tabvis.channels.plugins.teams.client import TeamsClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402


# --- test signing keys (a real RSA keypair; generated once to keep the suite fast) -------------

_CLIENT_ID = "cli_test"
_SERVICE_URL = "https://smba.trafficmanager.net/teams/"
_KID = "test-key-1"
_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # not in the JWKS


def _jwks(priv: rsa.RSAPrivateKey = _PRIV, *, kid: str = _KID) -> dict:
    numbers = priv.public_key().public_numbers()
    n = crypto.b64url_encode(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big"))
    e = crypto.b64url_encode(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big"))
    return {"keys": [{"kty": "RSA", "use": "sig", "kid": kid, "n": n, "e": e}]}


def _sign_jwt(claims: dict, *, priv: rsa.RSAPrivateKey = _PRIV, kid: str = _KID, alg: str = "RS256") -> str:
    def _seg(obj: dict) -> str:
        return crypto.b64url_encode(json.dumps(obj, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{_seg({'alg': alg, 'typ': 'JWT', 'kid': kid})}.{_seg(claims)}".encode("ascii")
    signature = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{crypto.b64url_encode(signature)}"


def _claims(*, aud: str = _CLIENT_ID, service_url: str = _SERVICE_URL, exp_delta: int = 3600) -> dict:
    now = int(time.time())
    return {
        "aud": aud,
        "iss": crypto.BOT_FRAMEWORK_ISSUER,
        "iat": now,
        "exp": now + exp_delta,
        "serviceurl": service_url,
    }


# --- fixtures / helpers ------------------------------------------------------------------------


def _config(**kw) -> TeamsConfig:
    base = dict(client_id=_CLIENT_ID, client_secret="secret_test", tenant_id="tenant-789")
    base.update(kw)
    return TeamsConfig(**base)


def _message_activity(
    activity_id: str,
    conversation_id: str,
    text: str,
    *,
    from_id: str = "29:user-1",
    from_aad: str = "aad-456",
    conversation_type: str = "personal",
    service_url: str = _SERVICE_URL,
) -> dict:
    return {
        "type": "message",
        "id": activity_id,
        "timestamp": "2026-07-22T10:00:00.000Z",
        "serviceUrl": service_url,
        "channelId": "msteams",
        "from": {"id": from_id, "name": "Test User", "aadObjectId": from_aad},
        "conversation": {"conversationType": conversation_type, "tenantId": "tenant-789", "id": conversation_id},
        "recipient": {"id": f"28:{_CLIENT_ID}", "name": "Hermes"},
        "text": text,
        "textFormat": "plain",
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _signed_headers(activity: dict, *, priv: rsa.RSAPrivateKey = _PRIV, kid: str = _KID, **claim_kw) -> dict:
    claim_kw.setdefault("service_url", str(activity.get("serviceUrl") or _SERVICE_URL))
    token = _sign_jwt(_claims(**claim_kw), priv=priv, kid=kid)
    return {"Authorization": f"Bearer {token}"}


class _FakeClient:
    """Stands in for TeamsClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.closed = False

    async def send_text(
        self, conversation_id: str, text: str, *, service_url: str | None = None, text_format: str = "markdown"
    ) -> str:
        self.calls.append((conversation_id, text, service_url))
        return "activity-sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> TeamsChannel:
    ch = TeamsChannel(_config(**cfg), client=fake if fake is not None else _FakeClient())
    ch.load_signing_keys(_jwks())  # the Bot Framework JWKS is injected rather than fetched over the net
    return ch


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_teams_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "teams"
    # Teams verifies its own webhooks (JWT Bearer), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: JWT bearer validation -------------------------------------------------------------


def test_validate_bearer_roundtrip_and_rejections() -> None:
    store = crypto.SigningKeyStore()
    store.load_jwks(_jwks())

    good = _sign_jwt(_claims())
    claims = crypto.validate_bearer(good, key_store=store, audience=_CLIENT_ID, service_url=_SERVICE_URL)
    assert claims["aud"] == _CLIENT_ID

    # wrong audience
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer(good, key_store=store, audience="someone-else", service_url=_SERVICE_URL)

    # signed by a key that is not in the JWKS -> signature does not verify
    forged = _sign_jwt(_claims(), priv=_OTHER_PRIV)
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer(forged, key_store=store, audience=_CLIENT_ID, service_url=_SERVICE_URL)

    # expired
    stale = _sign_jwt(_claims(exp_delta=-10_000))
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer(stale, key_store=store, audience=_CLIENT_ID, service_url=_SERVICE_URL)

    # serviceUrl anti-spoofing mismatch
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer(
            good, key_store=store, audience=_CLIENT_ID,
            service_url="https://smba.trafficmanager.net/other/",
        )

    # empty key store fails closed
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer(good, key_store=crypto.SigningKeyStore(), audience=_CLIENT_ID)

    # malformed token
    with pytest.raises(crypto.JwtError):
        crypto.validate_bearer("not-a-jwt", key_store=store, audience=_CLIENT_ID)


# --- webhook decoding: valid / rejected --------------------------------------------------------


def test_handle_webhook_accepts_valid_jwt() -> None:
    ch = _channel()
    activity = _message_activity("activity-001", "19:abc@thread.v2", "hi bot")
    result = ch.handle_webhook(_signed_headers(activity), _body(activity))
    assert result.challenge is None  # Teams has no challenge handshake
    assert result.raw is not None and not result.rejected
    assert result.raw.external_event_id == "activity-001"
    assert result.raw.external_conversation_id == "19:abc@thread.v2"


def test_handle_webhook_rejects_missing_bearer_token() -> None:
    ch = _channel()
    activity = _message_activity("activity-002", "19:c@thread.v2", "hi")
    result = ch.handle_webhook({}, _body(activity))
    assert result.rejected and result.raw is None


def test_handle_webhook_rejects_forged_signature() -> None:
    ch = _channel()
    activity = _message_activity("activity-003", "19:c@thread.v2", "hi")
    headers = _signed_headers(activity, priv=_OTHER_PRIV)  # not the key in the JWKS
    result = ch.handle_webhook(headers, _body(activity))
    assert result.rejected


def test_handle_webhook_rejects_wrong_audience() -> None:
    ch = _channel()
    activity = _message_activity("activity-004", "19:c@thread.v2", "hi")
    headers = _signed_headers(activity, aud="a-different-app")
    result = ch.handle_webhook(headers, _body(activity))
    assert result.rejected


def test_handle_webhook_rejects_invalid_json() -> None:
    result = _channel().handle_webhook({}, b"not json")
    assert result.rejected


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message() -> None:
    async def scenario() -> None:
        ch = _channel()
        activity = _message_activity("e1", "19:conv-1", "hello bot", from_aad="aad-user")
        raw = ch.handle_webhook(_signed_headers(activity), _body(activity)).raw
        (msg,) = await ch.normalize(raw)
        assert msg.text == "hello bot"
        assert msg.external_conversation_id == "19:conv-1"
        assert msg.external_event_id == "e1"
        assert msg.external_user_id == "aad-user"  # prefers the stable aadObjectId

    asyncio.run(scenario())


def test_normalize_strips_bot_mention() -> None:
    async def scenario() -> None:
        ch = _channel()
        activity = _message_activity("e2", "19:conv-1", "<at>Hermes</at> what is the weather?")
        (msg,) = await ch.normalize(ch.handle_webhook(_signed_headers(activity), _body(activity)).raw)
        assert msg.text == "what is the weather?"

    asyncio.run(scenario())


def test_normalize_falls_back_to_id_without_aad() -> None:
    async def scenario() -> None:
        ch = _channel()
        activity = _message_activity("e3", "19:conv-1", "hi", from_id="29:teams-only")
        del activity["from"]["aadObjectId"]
        (msg,) = await ch.normalize(ch.handle_webhook(_signed_headers(activity), _body(activity)).raw)
        assert msg.external_user_id == "29:teams-only"

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # the bot's own message (from.id is the channel-prefixed client id) is filtered
        own = _message_activity("e4", "19:conv-1", "loop?", from_id=f"28:{_CLIENT_ID}")
        assert await ch.normalize(ch.handle_webhook(_signed_headers(own), _body(own)).raw) == []
        # a non-message activity type produces nothing
        other = _message_activity("e5", "19:conv-1", "typing…")
        other["type"] = "typing"
        assert await ch.normalize(ch.handle_webhook(_signed_headers(other), _body(other)).raw) == []

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        teams = _channel()
        gw.register_plugin(teams)
        gw.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
        await gw.start_plugin("teams")

        activity = _message_activity("evt-1", "19:conv-A", "run this")
        raw = teams.handle_webhook(_signed_headers(activity), _body(activity)).raw
        (result,) = await gw.receive_webhook("ca_teams", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        teams = _channel()
        gw.register_plugin(teams)
        gw.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
        await gw.start_plugin("teams")

        activity = _message_activity("evt-dup", "19:conv-B", "hi")
        raw = teams.handle_webhook(_signed_headers(activity), _body(activity)).raw
        (first,) = await gw.receive_webhook("ca_teams", raw)
        (retry,) = await gw.receive_webhook("ca_teams", raw)  # Bot Framework re-delivers on retry
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
        teams = _channel(fake)
        gw.register_plugin(teams)
        gw.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
        await gw.start_plugin("teams")

        # An inbound message creates the conversation<->chat binding the outbound needs.
        activity = _message_activity("evt-1", "19:conv-send", "hello")
        raw = teams.handle_webhook(_signed_headers(activity), _body(activity)).raw
        (inbound,) = await gw.receive_webhook("ca_teams", raw)

        receipt = await gw.deliver(
            "ca_teams",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "activity-sent"
        # Sent to the right conversation, with the serviceUrl learned from the inbound activity.
        assert fake.calls == [("19:conv-send", "done", _SERVICE_URL)]

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        teams = _channel()
        gw.register_plugin(teams)
        gw.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
        await gw.start_plugin("teams")
        receipt = await gw.deliver(
            "ca_teams", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (token + send over a mock transport) ------------------------------------------


def test_client_fetches_token_then_sends() -> None:
    async def scenario() -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/oauth2/v2.0/token"):
                assert "Authorization" not in request.headers
                form = parse_qs(request.content.decode("utf-8"))
                assert form["grant_type"] == ["client_credentials"]
                assert form["scope"] == ["https://api.botframework.com/.default"]
                return httpx.Response(200, json={"access_token": "the-token", "expires_in": 3600})
            if "/v3/conversations/" in request.url.path and request.url.path.endswith("/activities"):
                assert request.headers["Authorization"] == "Bearer the-token"
                body = json.loads(request.content)
                assert body["type"] == "message"
                assert body["text"] == "hello cron"
                assert body["textFormat"] == "markdown"
                return httpx.Response(200, json={"id": "activity-xyz"})
            return httpx.Response(404, json={"error": {"message": "not found"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        tc = TeamsClient(_config(), client=client)
        message_id = await tc.send_text("19:conv@thread.v2", "hello cron")
        assert message_id == "activity-xyz"
        assert any(p.endswith("/oauth2/v2.0/token") for p in seen)
        await tc.aclose()

    asyncio.run(scenario())


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        teams = _channel(fake)
        gw.register_plugin(teams)
        gw.register_account(ChannelAccount(channel_account_id="ca_teams", plugin_id="teams"))
        assert (await teams.health()).status == "stopped"
        await gw.start_plugin("teams")
        assert (await teams.health()).status == "ready"
        await gw.registry.stop("teams")
        assert (await teams.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
