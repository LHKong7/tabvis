"""ChannelRuntime — mounting IM channels into the gateway: inbound ingress + outbound-on-completion."""

from __future__ import annotations

import asyncio
import json

from tabvis.channels.plugins.feishu import FeishuChannel, FeishuConfig
from tabvis.gateway.events.store import get_event_store
from tabvis.gateway.protocol.events import AGGREGATE_RUN, EventScope, EventType
from tabvis.gateway.runtime.channels import ChannelRuntime


def _text_event(event_id: str, chat_id: str, text: str) -> bytes:
    payload = {
        "schema": "2.0",
        "header": {"event_id": event_id, "event_type": "im.message.receive_v1", "app_id": "cli_test",
                   "token": "vtok"},
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_u"}},
            "message": {"message_id": f"om_{event_id}", "chat_id": chat_id, "message_type": "text",
                        "content": json.dumps({"text": text})},
        },
    }
    return json.dumps(payload).encode("utf-8")


class _FakeFeishuClient:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_text(self, receive_id, text, *, receive_id_type="chat_id") -> str:
        self.sent.append((receive_id, text))
        return "om_out"

    async def aclose(self) -> None:
        pass


def _runtime_with_feishu(fake: _FakeFeishuClient | None = None) -> ChannelRuntime:
    runtime = ChannelRuntime()
    feishu = FeishuChannel(FeishuConfig(app_id="cli_test", app_secret="sec", verification_token="vtok"),
                           client=fake if fake is not None else _FakeFeishuClient())
    runtime.register("feishu", feishu)
    return runtime


# --- config ------------------------------------------------------------------------------------


def test_from_env_none_when_disabled() -> None:
    assert ChannelRuntime.from_env(env={}) is None


def test_from_env_registers_and_records_errors() -> None:
    runtime = ChannelRuntime.from_env(env={
        "TABVIS_CHANNELS": "feishu, bogus",
        "TABVIS_FEISHU_APP_ID": "cli_x", "TABVIS_FEISHU_APP_SECRET": "sec",
    })
    assert runtime is not None
    assert runtime.has_channel("feishu")
    assert runtime.health()["feishu"] == "ready"
    assert runtime.health()["bogus"].startswith("error:")  # unknown plugin recorded, not fatal


# --- inbound -----------------------------------------------------------------------------------


def test_ingest_challenge() -> None:
    async def scenario() -> None:
        runtime = _runtime_with_feishu()
        await runtime.start()
        body = json.dumps({"type": "url_verification", "token": "vtok", "challenge": "c-1"}).encode()
        result = await runtime.ingest_webhook("feishu", {}, body)
        assert result == {"challenge": "c-1"}
        await runtime.stop()

    asyncio.run(scenario())


def test_ingest_text_creates_run() -> None:
    async def scenario() -> None:
        runtime = _runtime_with_feishu()
        await runtime.start()
        result = await runtime.ingest_webhook("feishu", {}, _text_event("e1", "oc_chat", "hello"))
        assert result["ok"] is True
        assert result["results"][0]["run_id"].startswith("run_")
        received = [e for e in get_event_store().read() if e.type == EventType.CONVERSATION_MESSAGE_RECEIVED]
        assert len(received) == 1
        await runtime.stop()

    asyncio.run(scenario())


# --- outbound (run.completed -> channel) -------------------------------------------------------


def test_run_completed_is_delivered_back_to_channel() -> None:
    async def scenario() -> None:
        fake = _FakeFeishuClient()
        runtime = _runtime_with_feishu(fake)
        await runtime.start()

        # An inbound message creates the conversation<->chat binding and a Run.
        result = await runtime.ingest_webhook("feishu", {}, _text_event("e1", "oc_chat", "question?"))
        run_id = result["results"][0]["run_id"]

        # The run finishes: the runner records the final text as result_preview on run.completed.
        get_event_store().append(
            AGGREGATE_RUN, run_id, EventType.RUN_COMPLETED, scope=EventScope(run_id=run_id),
            data={"result_preview": "the answer"},
        )
        await asyncio.sleep(0.05)  # let the outbound worker drain the queue

        assert fake.sent == [("oc_chat", "the answer")]  # delivered back to the originating chat
        await runtime.stop()

    asyncio.run(scenario())


def test_webhook_http_route_returns_challenge() -> None:
    # End-to-end through the mounted route: POST /v1/channels/{plugin}/webhook.
    from starlette.testclient import TestClient

    from tabvis.gateway.access.http import create_gateway_app

    app = create_gateway_app()
    app.state.gateway.channels = _runtime_with_feishu()
    body = json.dumps({"type": "url_verification", "token": "vtok", "challenge": "c-9"}).encode()
    with TestClient(app) as client:
        resp = client.post("/v1/channels/feishu/webhook", content=body)
    assert resp.status_code == 200 and resp.json() == {"challenge": "c-9"}

    # An unknown channel is a 404 (nothing configured for it).
    with TestClient(app) as client:
        assert client.post("/v1/channels/nope/webhook", content=b"{}").status_code == 404


def test_completion_for_non_channel_run_is_ignored() -> None:
    async def scenario() -> None:
        fake = _FakeFeishuClient()
        runtime = _runtime_with_feishu(fake)
        await runtime.start()
        # A run with no channel binding (e.g. created via /v1/runs) must not trigger a delivery.
        get_event_store().append(
            AGGREGATE_RUN, "run_orphan", EventType.RUN_COMPLETED, scope=EventScope(run_id="run_orphan"),
            data={"result_preview": "nobody home"},
        )
        await asyncio.sleep(0.02)
        assert fake.sent == []
        await runtime.stop()

    asyncio.run(scenario())
