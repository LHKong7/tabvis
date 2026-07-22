"""Google Chat channel plugin — crypto, webhook decoding, normalize, deliver, and end-to-end.

Mirrors ``test_feishu_channel.py``: exercises the plugin against the real ``ChannelGateway`` inbound
pipeline (dedupe → bind → message event → Run) and delivery path, plus Google Chat's own webhook
verification — a Google-issued OIDC ID token (RS256 JWT) in ``Authorization: Bearer``. Google Chat has
no ``url_verification`` challenge, no HMAC, and no AES envelope, so those Feishu cases have no analog
here; the verification cases cover a valid bearer and the ways a bearer is rejected instead.

Inbound tokens are minted in-test with a throwaway RSA key and verified through an injected static key
resolver (the production path would fetch Google's JWKS instead). Outbound goes over ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tabvis.channels.core.contract import OutboundMessage
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway
from tabvis.channels.plugins.google_chat import GoogleChatChannel, GoogleChatConfig
from tabvis.channels.plugins.google_chat import crypto
from tabvis.channels.plugins.google_chat.client import GoogleChatClient
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import EventType


# --- RSA key + JWT helpers (the test plays Google's signer) ------------------------------------


def _rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


def _private_pem(priv) -> str:
    from cryptography.hazmat.primitives import serialization

    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_id_token(priv, *, kid: str, claims: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = priv.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input}.{_b64(signature)}"


# Google's signing key (what our verifier trusts) and an unrelated key (a forged-token signer).
_PRIV, _PUB = _rsa_keypair()
_OTHER_PRIV, _ = _rsa_keypair()
_KID = "test-kid"
_AUDIENCE = "https://tabvis.example/google-chat/events"
_CALLER_EMAIL = "chat@system.gserviceaccount.com"


def _resolve(kid):
    return _PUB if kid == _KID else None


def _id_token(*, priv=_PRIV, kid=_KID, email=_CALLER_EMAIL, aud=_AUDIENCE, exp_delta=3600, iat_delta=0,
              iss="https://accounts.google.com") -> str:
    now = int(time.time())
    return _sign_id_token(
        priv,
        kid=kid,
        claims={"iss": iss, "aud": aud, "email": email, "email_verified": True,
                "iat": now + iat_delta, "exp": now + exp_delta},
    )


def _bearer(**kw) -> dict:
    return {"Authorization": "Bearer " + _id_token(**kw)}


# --- config / event / client helpers -----------------------------------------------------------


def _config(**kw) -> GoogleChatConfig:
    base = dict(
        service_account_email="bot@proj.iam.gserviceaccount.com",
        private_key="",  # only outbound needs a real key; verification tests don't send
        audience=_AUDIENCE,
        caller_service_account_emails=(_CALLER_EMAIL,),
    )
    base.update(kw)
    return GoogleChatConfig(**base)


def _message_event(name: str, space: str, text: str, *, sender_email: str = "alice@example.com",
                   sender_type: str = "HUMAN", argument_text: str | None = None) -> dict:
    return {
        "type": "MESSAGE",
        "message": {
            "name": name,
            "sender": {"name": "users/123", "email": sender_email, "displayName": "Alice", "type": sender_type},
            "text": text,
            "argumentText": argument_text if argument_text is not None else text,
            "thread": {"name": f"{space}/threads/T"},
            "space": {"name": space, "spaceType": "SPACE"},
        },
        "space": {"name": space, "spaceType": "SPACE"},
    }


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class _FakeClient:
    """Stands in for GoogleChatClient so deliver tests never touch the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def send_text(self, space_name: str, text: str, *, thread_name: str | None = None) -> str:
        self.calls.append((space_name, text))
        return "spaces/AAAA/messages/sent"

    async def aclose(self) -> None:
        self.closed = True


def _channel(fake: _FakeClient | None = None, **cfg) -> GoogleChatChannel:
    return GoogleChatChannel(
        _config(**cfg),
        client=fake if fake is not None else _FakeClient(),
        resolve_key=_resolve,
    )


# --- manifest ----------------------------------------------------------------------------------


def test_manifest_is_google_chat_and_unsigned_at_the_framework_level() -> None:
    ch = _channel()
    assert ch.manifest.plugin_id == "google_chat"
    # Google verifies its own webhooks (OIDC bearer), so the generic HMAC gate is off.
    assert ch.manifest.signed_webhooks is False
    assert "message.text.inbound" in ch.manifest.capabilities
    assert "message.text.outbound" in ch.manifest.capabilities


# --- crypto: id-token verification -------------------------------------------------------------


def test_verify_google_id_token_roundtrip_and_rejection() -> None:
    claims = crypto.verify_google_id_token(_id_token(), audience=_AUDIENCE, resolve_key=_resolve)
    assert claims["email"] == _CALLER_EMAIL

    # wrong audience
    with pytest.raises(crypto.InvalidGoogleToken):
        crypto.verify_google_id_token(_id_token(aud="https://evil.example"), audience=_AUDIENCE, resolve_key=_resolve)
    # expired (well past the clock-skew leeway)
    with pytest.raises(crypto.InvalidGoogleToken):
        crypto.verify_google_id_token(_id_token(exp_delta=-600, iat_delta=-3600), audience=_AUDIENCE, resolve_key=_resolve)
    # forged signature (signed by an unrelated key, but our resolver returns Google's key)
    with pytest.raises(crypto.InvalidGoogleToken):
        crypto.verify_google_id_token(_id_token(priv=_OTHER_PRIV), audience=_AUDIENCE, resolve_key=_resolve)
    # unknown issuer
    with pytest.raises(crypto.InvalidGoogleToken):
        crypto.verify_google_id_token(_id_token(iss="https://evil.example"), audience=_AUDIENCE, resolve_key=_resolve)
    # unknown signing key (kid not resolvable)
    with pytest.raises(crypto.InvalidGoogleToken):
        crypto.verify_google_id_token(_id_token(kid="rotated"), audience=_AUDIENCE, resolve_key=_resolve)


def test_service_account_assertion_is_a_verifiable_rs256_jwt() -> None:
    priv, pub = _rsa_keypair()
    assertion = crypto.sign_service_account_assertion(
        client_email="bot@proj.iam.gserviceaccount.com",
        private_key_pem=_private_pem(priv),
        private_key_id="pk-1",
        token_uri="https://oauth2.googleapis.com/token",
    )
    header, claims, signing_input, signature = crypto.decode_jwt(assertion)
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "pk-1"}
    assert claims["iss"] == "bot@proj.iam.gserviceaccount.com"
    assert claims["scope"] == crypto.CHAT_BOT_SCOPE
    assert claims["aud"] == "https://oauth2.googleapis.com/token"
    assert crypto._verify_rs256(pub, signing_input, signature)


# --- webhook decoding: bearer verification -----------------------------------------------------


def test_handle_webhook_accepts_valid_bearer() -> None:
    ch = _channel()
    body = _body(_message_event("spaces/A/messages/M.M", "spaces/A", "hi bot"))
    result = ch.handle_webhook(_bearer(), body)
    assert result.raw is not None and not result.rejected
    assert result.raw.external_event_id == "spaces/A/messages/M.M"
    assert result.raw.external_conversation_id == "spaces/A"
    assert result.challenge is None  # Google Chat has no challenge handshake


def test_handle_webhook_rejects_missing_and_malformed_bearer() -> None:
    ch = _channel()
    body = _body(_message_event("spaces/A/messages/M.M", "spaces/A", "hi"))
    assert ch.handle_webhook({}, body).rejected                                   # no header
    assert ch.handle_webhook({"Authorization": "Basic abc"}, body).rejected       # wrong scheme
    assert ch.handle_webhook({"Authorization": "Bearer "}, body).rejected         # empty token
    assert ch.handle_webhook({"Authorization": "Bearer not.a.jwt"}, body).rejected  # undecodable


def test_handle_webhook_rejects_forged_and_wrong_identity() -> None:
    ch = _channel()
    body = _body(_message_event("spaces/A/messages/M.M", "spaces/A", "hi"))
    # a token Google never signed
    assert ch.handle_webhook(_bearer(priv=_OTHER_PRIV), body).rejected
    # a valid Google token whose caller SA email is not on our allowlist
    reject = ch.handle_webhook(_bearer(email="stranger@evil.gserviceaccount.com"), body)
    assert reject.rejected and reject.reason == "unexpected_google_bearer_identity"


def test_handle_webhook_rejects_when_verification_not_configured() -> None:
    # No caller SA email configured -> verification is inactive and every request is rejected.
    ch = _channel(caller_service_account_emails=())
    body = _body(_message_event("spaces/A/messages/M.M", "spaces/A", "hi"))
    result = ch.handle_webhook(_bearer(), body)
    assert result.rejected and result.reason == "google_chat_http_events_not_configured"


# --- normalize ---------------------------------------------------------------------------------


def test_normalize_text_message_prefers_argument_text() -> None:
    async def scenario() -> None:
        ch = _channel()
        # In a group space Google sends the mention in ``text`` but strips it from ``argumentText``.
        event = _message_event("spaces/A/messages/M.M", "spaces/A", "@Tabvis do the thing",
                               argument_text=" do the thing")
        (msg,) = await ch.normalize(ch.handle_webhook(_bearer(), _body(event)).raw)
        assert msg.text == "do the thing"                       # mention stripped, trimmed
        assert msg.external_conversation_id == "spaces/A"
        assert msg.external_event_id == "spaces/A/messages/M.M"
        assert msg.external_user_id == "alice@example.com"      # sender email is the canonical id

    asyncio.run(scenario())


def test_normalize_ignores_bot_and_non_message_events() -> None:
    async def scenario() -> None:
        ch = _channel()
        # the bot's own (or another bot's) message
        bot = _message_event("spaces/A/messages/B.B", "spaces/A", "loop?", sender_type="BOT")
        assert await ch.normalize(ch.handle_webhook(_bearer(), _body(bot)).raw) == []
        # a non-MESSAGE event type
        other = {"type": "ADDED_TO_SPACE", "space": {"name": "spaces/A"}}
        assert await ch.normalize(ch.handle_webhook(_bearer(), _body(other)).raw) == []

    asyncio.run(scenario())


def test_normalize_reads_workspace_addon_envelope() -> None:
    async def scenario() -> None:
        ch = _channel()
        inner = _message_event("spaces/A/messages/M.M", "spaces/A", "hello")
        envelope = {"chat": {"messagePayload": {"message": inner["message"], "space": inner["space"]}}}
        (msg,) = await ch.normalize(ch.handle_webhook(_bearer(), _body(envelope)).raw)
        assert msg.text == "hello" and msg.external_conversation_id == "spaces/A"

    asyncio.run(scenario())


# --- end to end through the gateway ------------------------------------------------------------


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        gchat = _channel()
        gw.register_plugin(gchat)
        gw.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
        await gw.start_plugin("google_chat")

        raw = gchat.handle_webhook(_bearer(), _body(_message_event("spaces/A/messages/E.1", "spaces/A", "run this"))).raw
        (result,) = await gw.receive_webhook("ca_gchat", raw)

        assert result.run_id and result.run_id.startswith("run_")
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_is_idempotent() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        gchat = _channel()
        gw.register_plugin(gchat)
        gw.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
        await gw.start_plugin("google_chat")

        raw = gchat.handle_webhook(_bearer(), _body(_message_event("spaces/A/messages/DUP", "spaces/B", "hi"))).raw
        (first,) = await gw.receive_webhook("ca_gchat", raw)
        (retry,) = await gw.receive_webhook("ca_gchat", raw)  # Google re-delivers the same message
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        received = [
            e for e in get_event_store().read(aggregate_id=first.conversation_id)
            if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED
        ]
        assert len(received) == 1

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_deliver_resolves_space_and_sends_text() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        gchat = _channel(fake)
        gw.register_plugin(gchat)
        gw.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
        await gw.start_plugin("google_chat")

        # An inbound message creates the conversation<->space binding the outbound needs.
        raw = gchat.handle_webhook(_bearer(), _body(_message_event("spaces/A/messages/E.1", "spaces/send", "hello"))).raw
        (inbound,) = await gw.receive_webhook("ca_gchat", raw)

        receipt = await gw.deliver(
            "ca_gchat",
            OutboundMessage(delivery_id="dlv-1", conversation_id=inbound.conversation_id, run_id=inbound.run_id, text="done"),
        )
        assert receipt.status == "succeeded"
        assert receipt.external_message_id == "spaces/AAAA/messages/sent"
        assert fake.calls == [("spaces/send", "done")]  # sent to the right space with the right text

    asyncio.run(scenario())


def test_deliver_fails_gracefully_without_binding() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        gchat = _channel()
        gw.register_plugin(gchat)
        gw.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
        await gw.start_plugin("google_chat")
        receipt = await gw.deliver(
            "ca_gchat", OutboundMessage(delivery_id="dlv-x", conversation_id="conv_unknown", run_id=None, text="hi")
        )
        assert receipt.status == "failed"

    asyncio.run(scenario())


# --- REST client (SA token + send over a mock transport) ---------------------------------------


def test_client_mints_token_then_sends() -> None:
    async def scenario() -> None:
        priv, _ = _rsa_keypair()
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host + request.url.path)
            if request.url.host == "oauth2.googleapis.com":
                assert "Authorization" not in request.headers  # the assertion IS the credential
                form = parse_qs(request.content.decode("utf-8"))
                assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
                assert form["assertion"][0].count(".") == 2      # a compact JWT
                return httpx.Response(200, json={"access_token": "at-xyz", "expires_in": 3600})
            if request.url.path.endswith("/messages"):
                assert request.url.path == "/v1/spaces/AAAA/messages"
                assert request.headers["Authorization"] == "Bearer at-xyz"
                assert json.loads(request.content) == {"text": "hi there"}
                return httpx.Response(200, json={"name": "spaces/AAAA/messages/M.1"})
            return httpx.Response(404, json={"error": {"code": 404, "message": "not found"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gc = GoogleChatClient(_config(private_key=_private_pem(priv), private_key_id="pk-1"), client=client)
        message_name = await gc.send_text("spaces/AAAA", "hi there")
        assert message_name == "spaces/AAAA/messages/M.1"
        assert any(h.startswith("oauth2.googleapis.com") for h in seen)
        await gc.aclose()

    asyncio.run(scenario())


def test_client_send_raises_on_google_error() -> None:
    async def scenario() -> None:
        priv, _ = _rsa_keypair()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "oauth2.googleapis.com":
                return httpx.Response(200, json={"access_token": "at-xyz", "expires_in": 3600})
            return httpx.Response(403, json={"error": {"code": 403, "message": "bot removed from space"}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        gc = GoogleChatClient(_config(private_key=_private_pem(priv)), client=client)
        with pytest.raises(Exception):
            await gc.send_text("spaces/AAAA", "hi")
        await gc.aclose()

    asyncio.run(scenario())


# --- config from env ---------------------------------------------------------------------------


def test_config_from_env_parses_inline_service_account_json() -> None:
    priv, _ = _rsa_keypair()
    sa = {
        "type": "service_account",
        "client_email": "bot@proj.iam.gserviceaccount.com",
        "private_key": _private_pem(priv),
        "private_key_id": "pk-9",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    env = {
        "TABVIS_GOOGLE_CHAT_SERVICE_ACCOUNT_JSON": json.dumps(sa),
        "TABVIS_GOOGLE_CHAT_WEBHOOK_URL": "https://tabvis.example/gc",
        "TABVIS_GOOGLE_CHAT_SA_EMAIL": "a@sys.gserviceaccount.com, b@sys.gserviceaccount.com",
    }
    cfg = GoogleChatConfig.from_env(env)
    assert cfg.service_account_email == "bot@proj.iam.gserviceaccount.com"
    assert cfg.private_key_id == "pk-9"
    assert cfg.audience == "https://tabvis.example/gc"  # defaults to the webhook URL
    assert cfg.caller_service_account_emails == ("a@sys.gserviceaccount.com", "b@sys.gserviceaccount.com")


# --- lifecycle ---------------------------------------------------------------------------------


def test_plugin_lifecycle() -> None:
    async def scenario() -> None:
        gw = ChannelGateway()
        fake = _FakeClient()
        gchat = _channel(fake)
        gw.register_plugin(gchat)
        gw.register_account(ChannelAccount(channel_account_id="ca_gchat", plugin_id="google_chat"))
        assert (await gchat.health()).status == "stopped"
        await gw.start_plugin("google_chat")
        assert (await gchat.health()).status == "ready"
        await gw.registry.stop("google_chat")
        assert (await gchat.health()).status == "stopped"
        assert fake.closed is True  # stop() closed the API client

    asyncio.run(scenario())
