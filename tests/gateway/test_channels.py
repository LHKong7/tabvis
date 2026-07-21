"""Phase 4 — Channel Framework: bindings, inbound idempotency, signatures, delivery (design §4, §15)."""

from __future__ import annotations

import asyncio
import json

import pytest

from tabvis.channels.core import signatures
from tabvis.channels.core.contract import CAP_STREAM_INCREMENTAL, OutboundMessage, RawInbound
from tabvis.channels.core.identity import ChannelAccount
from tabvis.channels.core.service import ChannelGateway, InMemorySecretResolver
from tabvis.channels.core.stores import BindingStore
from tabvis.channels.plugins.example_webhook import ExampleWebhookChannel
from tabvis.channels.web.channel import WebChannel
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import EventType


# --- binding store -----------------------------------------------------------------------------


def test_binding_resolve_or_create_is_idempotent() -> None:
    store = BindingStore()
    first = store.resolve_or_create("ca_1", "thread-42")
    assert first.created is True
    again = store.resolve_or_create("ca_1", "thread-42")
    assert again.created is False
    assert again.binding.conversation_id == first.binding.conversation_id
    assert again.binding.agent_id == first.binding.agent_id  # same internal agent for the thread
    # a different external thread on the same account is a different conversation.
    other = store.resolve_or_create("ca_1", "thread-99")
    assert other.binding.conversation_id != first.binding.conversation_id


# --- signature primitive -----------------------------------------------------------------------


def test_signature_verify_roundtrip_and_rejection() -> None:
    body = b'{"hello":"world"}'
    good = signatures.sign("shh", body)
    assert signatures.verify("shh", body, good)
    assert signatures.verify("shh", body, f"sha256={good}")  # provider prefix accepted
    assert not signatures.verify("shh", body, "deadbeef")
    assert not signatures.verify("wrong", body, good)
    assert not signatures.verify("shh", body, None)


# --- inbound over the webhook proof channel ----------------------------------------------------


def _webhook_gateway() -> tuple[ChannelGateway, ExampleWebhookChannel, str]:
    secrets = InMemorySecretResolver({"cred/webhook": "topsecret"})
    gw = ChannelGateway(secret_resolver=secrets)
    plugin = ExampleWebhookChannel()
    gw.register_plugin(plugin)
    account = ChannelAccount(
        channel_account_id="ca_wh", plugin_id="example_webhook",
        external_account_ref="acct-1", credential_ref="cred/webhook", capabilities=list(plugin.manifest.capabilities),
    )
    gw.register_account(account)
    return gw, plugin, "topsecret"


def _signed_raw(secret: str, event_id: str, conversation: str, text: str) -> RawInbound:
    body_dict = {"event_id": event_id, "conversation": conversation, "text": text, "user": "u1"}
    body = json.dumps(body_dict).encode("utf-8")
    return RawInbound(
        external_event_id=event_id,
        external_conversation_id=conversation,
        external_account_ref="acct-1",
        payload=body_dict,
        signature=signatures.sign(secret, body),
        raw_body=body,
    )


def test_webhook_creates_message_event_and_run() -> None:
    async def scenario() -> None:
        gw, plugin, secret = _webhook_gateway()
        await gw.start_plugin("example_webhook")
        raw = _signed_raw(secret, "evt-1", "conv-A", "hello")
        (result,) = await gw.receive_webhook("ca_wh", raw)

        assert result.run_id and result.run_id.startswith("run_")
        assert plugin.acknowledged == ["evt-1"]
        types = [e.type for e in get_event_store().read(aggregate_id=result.conversation_id)]
        assert EventType.CONVERSATION_CREATED in types
        assert EventType.CONVERSATION_MESSAGE_RECEIVED in types

    asyncio.run(scenario())


def test_webhook_retry_creates_one_message_and_one_run() -> None:
    # design §15 Phase 4 acceptance: external webhook retries create one internal message and one Run.
    async def scenario() -> None:
        gw, plugin, secret = _webhook_gateway()
        await gw.start_plugin("example_webhook")
        raw = _signed_raw(secret, "evt-dup", "conv-B", "hi")

        (first,) = await gw.receive_webhook("ca_wh", raw)
        (retry,) = await gw.receive_webhook("ca_wh", raw)  # provider re-delivers the same event
        assert retry.duplicate is True
        assert retry.run_id == first.run_id

        # exactly one message.received event, and one run for the conversation.
        received = [e for e in get_event_store().read(aggregate_id=first.conversation_id)
                    if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1

    asyncio.run(scenario())


def test_webhook_bad_signature_is_rejected_with_no_side_effects() -> None:
    async def scenario() -> None:
        gw, plugin, _ = _webhook_gateway()
        await gw.start_plugin("example_webhook")
        raw = _signed_raw("wrong-secret", "evt-x", "conv-C", "nope")
        with pytest.raises(GatewayError) as ei:
            await gw.receive_webhook("ca_wh", raw)
        assert ei.value.code == "FORBIDDEN"
        assert plugin.acknowledged == []  # nothing processed

    asyncio.run(scenario())


# --- WebChannel and the "same Run from either channel" acceptance ------------------------------


def test_web_and_webhook_channels_both_originate_runs() -> None:
    # design §15 Phase 4 acceptance: the same Run flow can originate from WebChannel or the proof channel.
    async def scenario() -> None:
        secrets = InMemorySecretResolver({"cred/webhook": "topsecret"})
        gw = ChannelGateway(secret_resolver=secrets)
        web = WebChannel()
        webhook = ExampleWebhookChannel()
        gw.register_plugin(web)
        gw.register_plugin(webhook)
        gw.register_account(ChannelAccount(channel_account_id="ca_web", plugin_id="web"))
        gw.register_account(ChannelAccount(
            channel_account_id="ca_wh", plugin_id="example_webhook", credential_ref="cred/webhook",
        ))
        await gw.start_plugin("web")
        await gw.start_plugin("example_webhook")

        web_result = await web.submit_console_message("ca_web", "console-1", "run this")
        raw = _signed_raw("topsecret", "evt-1", "conv-1", "run that")
        (wh_result,) = await gw.receive_webhook("ca_wh", raw)

        assert web_result.run_id and wh_result.run_id
        assert web_result.run_id != wh_result.run_id  # independent runs, independent conversations

    asyncio.run(scenario())


# --- delivery ----------------------------------------------------------------------------------


def test_delivery_is_idempotent_on_delivery_id() -> None:
    async def scenario() -> None:
        gw, plugin, _ = _webhook_gateway()
        await gw.start_plugin("example_webhook")
        outbound = OutboundMessage(delivery_id="dlv_1", conversation_id="conv", run_id="run_1", text="done", final=True)

        first = await gw.deliver("ca_wh", outbound)
        assert first.status == "succeeded"
        again = await gw.deliver("ca_wh", OutboundMessage(delivery_id="dlv_1", conversation_id="conv", run_id="run_1", text="done"))
        assert again.status == "duplicate"
        assert len(plugin.delivered) == 1  # the plugin was invoked only once

    asyncio.run(scenario())


def test_non_streaming_channel_skips_partial_deliveries() -> None:
    # capability degradation (design §4.4): the webhook channel lacks stream.incremental.
    async def scenario() -> None:
        gw, plugin, _ = _webhook_gateway()
        await gw.start_plugin("example_webhook")
        assert CAP_STREAM_INCREMENTAL not in plugin.manifest.capabilities
        partial = OutboundMessage(delivery_id="dlv_p", conversation_id="conv", run_id="run_1", text="typ", final=False)
        receipt = await gw.deliver("ca_wh", partial)
        assert receipt.status == "skipped"
        assert plugin.delivered == []  # the partial never reached the channel

    asyncio.run(scenario())


def test_streaming_channel_receives_partials() -> None:
    async def scenario() -> None:
        secrets = InMemorySecretResolver()
        gw = ChannelGateway(secret_resolver=secrets)
        web = WebChannel()
        gw.register_plugin(web)
        gw.register_account(ChannelAccount(channel_account_id="ca_web", plugin_id="web"))
        await gw.start_plugin("web")
        partial = OutboundMessage(delivery_id="dlv_s", conversation_id="conv", run_id="run_1", text="typ", final=False)
        receipt = await gw.deliver("ca_web", partial)
        assert receipt.status == "succeeded"  # WebChannel has stream.incremental

    asyncio.run(scenario())


# --- registry lifecycle ------------------------------------------------------------------------


def test_plugin_registry_lifecycle() -> None:
    async def scenario() -> None:
        gw, plugin, _ = _webhook_gateway()
        assert gw.registry.status("example_webhook") == "configured"
        await gw.start_plugin("example_webhook")
        assert gw.registry.status("example_webhook") == "ready"
        assert (await gw.registry.health("example_webhook")).status == "ready"
        await gw.registry.stop("example_webhook")
        assert gw.registry.status("example_webhook") == "stopped"

    asyncio.run(scenario())
