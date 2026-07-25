"""Unified DLP Gateway + cleaners (design §11.1, §11.2, §11.3, §16.1)."""

from __future__ import annotations

from datetime import datetime, timezone
import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from tabvis.agent.tool_services.tool_execution import run_tool_use
from tabvis.authentication.models import CredentialCapability, ResolvedCredentials
from tabvis.authentication.secrets import secret_from_str
from tabvis.dlp import canary
from tabvis.dlp.gateway import DLPGateway, set_dlp_gateway
from tabvis.dlp.text import mask_identifiers, redact_headers, redact_mapping
from tabvis.dlp.url import clean_url
from tabvis.tool import Tool, ToolResult, ToolUseContext, ToolUseContextOptions


@pytest.fixture(autouse=True)
def _clean():
    canary.clear()
    set_dlp_gateway(None)
    yield
    canary.clear()
    set_dlp_gateway(None)


# --------------------------------------------------------------------------- cleaners


def test_clean_url_strips_userinfo_fragment_query_values() -> None:
    out = clean_url("https://user:pass@ex.com/login?token=abc123&x=1#frag")
    assert "user" not in out and "pass" not in out
    assert "abc123" not in out and "#frag" not in out
    assert out.startswith("https://ex.com/login")
    assert "token=" in out and "x=" in out  # keys kept, values dropped


def test_redact_headers() -> None:
    out = redact_headers({"Cookie": "sid=1", "Authorization": "Bearer x", "Accept": "text/html"})
    assert out["Cookie"] == "[redacted]"
    assert out["Authorization"] == "[redacted]"
    assert out["Accept"] == "text/html"


def test_mask_identifiers() -> None:
    assert "alice@example.com" not in mask_identifiers("mail alice@example.com now")
    assert "555" not in mask_identifiers("call +1 555-123-4567 please")


def test_mask_identifiers_preserves_numeric_web_identifiers() -> None:
    alibaba = "https://home.alibabagroup.com/en-US/document-1991237455038119936"
    sec = "https://www.sec.gov/Archives/edgar/data/1577551/000110465926000001/report.htm"
    html = '<a href="/en-US/document-1991237455038119936">report</a>'
    assert mask_identifiers(alibaba) == alibaba
    assert mask_identifiers(sec) == sec
    assert mask_identifiers(html) == html


def test_gateway_preserves_numeric_document_paths_in_nested_browser_data() -> None:
    url = "https://home.alibabagroup.com/en-US/document-1991237455038119936"
    decision = DLPGateway().scrub(
        "model_request",
        {
            "snapshot": f'- link "Press Releases" href="{url}"',
            "links": [{"text": "Press Releases", "href": url}],
        },
    )
    assert not decision.blocked
    assert decision.payload["snapshot"].endswith(f'href="{url}"')
    assert decision.payload["links"][0]["href"] == url


def test_redact_mapping_sensitive_keys() -> None:
    out = redact_mapping({"password": "hunter2", "user": "alice", "nested": {"api_key": "k"}})
    assert out["password"] == "[redacted]"
    assert out["nested"]["api_key"] == "[redacted]"
    assert out["user"] == "alice"


# --------------------------------------------------------------------------- gateway


def test_gateway_passes_clean_payload() -> None:
    gw = DLPGateway()
    decision = gw.scrub("log", {"msg": "hello", "url": "https://ex.com/a?t=secret"})
    assert not decision.blocked
    assert decision.payload["url"] == "https://ex.com/a?t="


def test_gateway_blocks_on_canary() -> None:
    canary.register(b"CanarySecretValue1", tag="password:p1")
    blocked_events = []
    gw = DLPGateway(on_secret_blocked=blocked_events.append)
    decision = gw.scrub("model_request", {"page": "the token is CanarySecretValue1 haha"})
    assert decision.blocked
    assert decision.payload is None
    assert decision.fingerprint is not None
    # a dlp.secret_blocked event fired, containing only the one-way fingerprint (no secret)
    assert len(blocked_events) == 1
    assert blocked_events[0].event == "dlp.secret_blocked"
    assert "CanarySecretValue1" not in blocked_events[0].fingerprint


def test_gateway_blocks_secret_value_object() -> None:
    gw = DLPGateway()
    decision = gw.scrub("transcript", {"leak": secret_from_str("hunter2xyz")})
    assert decision.blocked
    assert decision.fingerprint == "forbidden-object"


def test_gateway_blocks_resolved_credentials_object() -> None:
    gw = DLPGateway()
    rc = ResolvedCredentials(password=secret_from_str("hunter2xyz"))
    assert gw.scrub("artifact", rc).blocked


def test_gateway_blocks_capability_object() -> None:
    gw = DLPGateway()
    cap = CredentialCapability(
        id="cap_1",
        credential_profile_id="p1",
        browser_session_id="b1",
        task_id="t1",
        user_id="u1",
        top_level_origin="https://x.com",
        frame_origin="https://x.com",
        page_id="pg",
        navigation_generation=1,
        issued_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    assert gw.scrub("telemetry", {"cap": cap}).blocked


def test_gateway_redacts_headers_and_cookies() -> None:
    gw = DLPGateway()
    decision = gw.scrub("api", {"Cookie": "sid=abc", "Authorization": "Bearer t", "path": "/x"})
    assert not decision.blocked
    assert decision.payload["Cookie"] == "[redacted]"
    assert decision.payload["Authorization"] == "[redacted]"


def test_gateway_redacts_mixed_header_and_sensitive_mapping() -> None:
    gw = DLPGateway()
    decision = gw.scrub(
        "api",
        {"Cookie": "sid=abc", "password": "hunter2", "nested": {"api_key": "key"}},
    )
    assert decision.payload == {
        "Cookie": "[redacted]",
        "password": "[redacted]",
        "nested": {"api_key": "[redacted]"},
    }


def test_block_hook_failure_does_not_unblock() -> None:
    canary.register(b"AnotherCanary9", tag="x")

    def boom(_event):
        raise RuntimeError("hook down")

    gw = DLPGateway(on_secret_blocked=boom)
    decision = gw.scrub("crash_report", "leak AnotherCanary9 here")
    assert decision.blocked  # a broken hook still results in a block, never a passthrough


def test_tool_result_is_blocked_before_mapper_and_model_request() -> None:
    class Input(BaseModel):
        pass

    class LeakingTool(Tool):
        name = "Leaking"
        input_schema = Input
        max_result_size_chars = 100
        mapper_called = False

        async def call(self, *args, **kwargs):
            return ToolResult(data={"text": "contains CanarySecretValue1"})

        async def description(self, input, options):
            return self.name

        async def prompt(self, options):
            return self.name

        def map_tool_result_to_tool_result_block_param(self, content: Any, tool_use_id: str):
            self.mapper_called = True
            raise AssertionError("DLP must run before mapper serialization")

    tool = LeakingTool()
    canary.register(b"CanarySecretValue1", tag="password:p1")
    context = ToolUseContext(options=ToolUseContextOptions(tools=[tool]))

    async def allow(*_args):
        return {"behavior": "allow"}

    async def scenario():
        return [
            update
            async for update in run_tool_use(
                {"id": "tu1", "name": tool.name, "input": {}},
                {"uuid": "assistant-1"},
                allow,
                context,
            )
        ]

    updates = asyncio.run(scenario())
    block = updates[-1]["message"]["message"]["content"][0]
    assert block["is_error"] is True
    assert "DLP blocked" in block["content"]
    assert not tool.mapper_called
