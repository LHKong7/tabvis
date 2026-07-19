"""Tests for the model-provider abstraction (tabvis.agent.api.providers).

The query pipeline is Anthropic-native; a provider translates Anthropic request params ↔ its vendor
API and its stream back into Anthropic-shaped parts. These tests cover the pure converters (request
build + stream translate) with hand-built chunk objects — no SDKs, no network, no API keys — plus
provider selection. A tiny reconstructor mimics what model_client's loop does with the parts.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as N
from typing import Any

import pytest

from tabvis.agent.api.providers import (
    ModelGateway,
    ProtocolAdapter,
    get_model_gateway,
    get_model_provider,
    resolve_provider_name,
    strip_provider_prefix,
)
from tabvis.agent.api.providers import gemini_provider as gp
from tabvis.agent.api.providers import openai_provider as op


# --------------------------------------------------------------------------- selection


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-8", "anthropic"),
        ("gpt-4o", "openai"),
        ("o3-mini", "openai"),
        ("chatgpt-4o-latest", "openai"),
        ("gemini-2.0-flash", "gemini"),
        ("models/gemini-1.5-pro", "gemini"),
        ("openai/gpt-4o", "openai"),
        ("gemini/gemini-1.5-pro", "gemini"),
        ("something-unknown", "anthropic"),
    ],
)
def test_resolve_provider_name(model: str, expected: str) -> None:
    assert resolve_provider_name(model) == expected


def test_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABVIS_MODEL_PROVIDER", "gemini")
    assert resolve_provider_name("gpt-4o") == "gemini"  # env beats model-id inference


def test_strip_provider_prefix() -> None:
    assert strip_provider_prefix("openai/gpt-4o") == "gpt-4o"
    assert strip_provider_prefix("gpt-4o") == "gpt-4o"
    assert strip_provider_prefix("gemini/gemini-1.5-pro") == "gemini-1.5-pro"


# --------------------------------------------------------------------------- a reconstructor

def _reconstruct(events: list[dict]) -> dict[str, Any]:
    """Fold Anthropic-shaped parts the way model_client's loop does, for assertions."""
    text = ""
    tools: list[dict[str, Any]] = []
    blocks: dict[int, dict[str, Any]] = {}
    stop = None
    usage: dict[str, Any] = {}
    for e in events:
        t = e["type"]
        if t == "content_block_start":
            blocks[e["index"]] = dict(e["content_block"])
        elif t == "content_block_delta":
            d = e["delta"]
            if d["type"] == "text_delta":
                text += d["text"]
            elif d["type"] == "input_json_delta":
                b = blocks[e["index"]]
                b["input"] = (b.get("input") or "") + d["partial_json"]
        elif t == "content_block_stop":
            b = blocks.get(e["index"])
            if b and b.get("type") == "tool_use":
                tools.append(b)
        elif t == "message_delta":
            stop = e["delta"]["stop_reason"]
            usage = dict(e["usage"])
    return {"text": text, "tools": tools, "stop": stop, "usage": usage}


# --------------------------------------------------------------------------- OpenAI


def test_openai_request_build() -> None:
    params = {
        "model": "openai/gpt-4o",
        "max_tokens": 1024,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "t1", "name": "getw", "input": {"city": "SF"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "sunny"}]},
        ],
        "tools": [{"name": "getw", "description": "w", "input_schema": {"type": "object"}}],
    }
    req = op.build_openai_request(params)
    assert req["model"] == "gpt-4o"          # provider prefix stripped
    assert req["max_tokens"] == 1024
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    asst = req["messages"][2]
    assert asst["tool_calls"][0]["function"]["name"] == "getw"
    assert json.loads(asst["tool_calls"][0]["function"]["arguments"]) == {"city": "SF"}
    assert req["messages"][3] == {"role": "tool", "tool_call_id": "t1", "content": "sunny"}
    assert req["tools"][0]["type"] == "function"


def test_openai_assistant_toolcall_content_is_null() -> None:
    """OpenAI wants content=null (not "") for a tool-only assistant turn."""
    m = op._convert_message(
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {}}]}
    )
    assert m[0]["content"] is None and "tool_calls" in m[0]


def test_openai_stream_translation() -> None:
    async def chunks():
        yield N(id="c", choices=[N(delta=N(content="Hi ", tool_calls=None), finish_reason=None)], usage=None)
        yield N(id="c", choices=[N(delta=N(content="there", tool_calls=None), finish_reason=None)], usage=None)
        yield N(id="c", choices=[N(delta=N(content=None, tool_calls=[N(index=0, id="c1", function=N(name="getw", arguments='{"ci'))]), finish_reason=None)], usage=None)
        yield N(id="c", choices=[N(delta=N(content=None, tool_calls=[N(index=0, id=None, function=N(name=None, arguments='ty":"SF"}'))]), finish_reason="tool_calls")], usage=None)
        yield N(id="c", choices=[], usage=N(prompt_tokens=12, completion_tokens=7, prompt_tokens_details=None))

    async def run():
        return [e async for e in op.translate_openai_stream(chunks(), model="gpt-4o")]

    events = asyncio.run(run())
    assert events[0]["type"] == "message_start"
    assert events[-1]["type"] == "message_stop"
    r = _reconstruct(events)
    assert r["text"] == "Hi there"
    assert r["tools"][0]["name"] == "getw"
    assert json.loads(r["tools"][0]["input"]) == {"city": "SF"}
    assert r["stop"] == "tool_use"
    assert r["usage"]["input_tokens"] == 12 and r["usage"]["output_tokens"] == 7


# --------------------------------------------------------------------------- Gemini


def test_gemini_request_build_maps_tool_id_to_name_and_cleans_schema() -> None:
    params = {
        "model": "gemini/gemini-2.0-flash",
        "max_tokens": 2048,
        "system": "be terse",
        "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t9", "name": "getw", "input": {"city": "SF"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t9", "content": "sunny"}]},
        ],
        "tools": [{"name": "getw", "input_schema": {"type": "object", "$schema": "x", "additionalProperties": False}}],
    }
    req = gp.build_gemini_request(params)
    assert req["model"] == "gemini-2.0-flash"
    assert req["config"]["system_instruction"] == "be terse"
    assert req["contents"][0]["role"] == "model"  # assistant -> model
    assert req["contents"][0]["parts"][0]["function_call"]["name"] == "getw"
    # tool_result names its response via the id->name map, and role is user
    fr = req["contents"][1]["parts"][0]["function_response"]
    assert req["contents"][1]["role"] == "user" and fr["name"] == "getw" and fr["response"] == {"result": "sunny"}
    # JSON-schema keys Gemini rejects are stripped
    schema = req["config"]["tools"][0]["function_declarations"][0]["parameters"]
    assert "$schema" not in schema and "additionalProperties" not in schema


def test_gemini_stream_translation() -> None:
    async def chunks():
        yield N(candidates=[N(content=N(parts=[N(text="It is ", function_call=None)]), finish_reason=None)], usage_metadata=None)
        yield N(candidates=[N(content=N(parts=[N(text="sunny", function_call=None)]), finish_reason=None)], usage_metadata=None)
        yield N(
            candidates=[N(content=N(parts=[N(text=None, function_call=N(name="getw", args={"city": "SF"}))]), finish_reason="STOP")],
            usage_metadata=N(prompt_token_count=20, candidates_token_count=8, cached_content_token_count=3),
        )

    async def run():
        return [e async for e in gp.translate_gemini_stream(chunks(), model="gemini-2.0-flash")]

    events = asyncio.run(run())
    r = _reconstruct(events)
    assert r["text"] == "It is sunny"
    assert r["tools"][0]["name"] == "getw"
    assert json.loads(r["tools"][0]["input"]) == {"city": "SF"}
    assert r["stop"] == "tool_use"
    assert r["usage"]["input_tokens"] == 20 and r["usage"]["cache_read_input_tokens"] == 3


def test_gemini_text_only_finish_maps_to_end_turn() -> None:
    async def chunks():
        yield N(candidates=[N(content=N(parts=[N(text="hello", function_call=None)]), finish_reason="STOP")], usage_metadata=None)

    async def run():
        return [e async for e in gp.translate_gemini_stream(chunks(), model="g")]

    r = _reconstruct(asyncio.run(run()))
    assert r["text"] == "hello" and r["stop"] == "end_turn" and r["tools"] == []


# --------------------------------------------------------------------------- the three patterns


def test_strategy_selection_returns_the_right_provider() -> None:
    """STRATEGY: get_model_provider yields an interchangeable backend object per model."""
    assert get_model_provider("claude-opus-4-8").name == "anthropic"
    assert get_model_provider("gpt-4o").name == "openai"
    assert get_model_provider("gemini-2.0-flash").name == "gemini"


def test_adapters_satisfy_the_protocol_adapter_interface() -> None:
    """ADAPTER: the OpenAI/Gemini adapters implement adapt_request + adapt_stream."""
    for adapter in (op.OpenAIAdapter(), gp.GeminiAdapter()):
        assert isinstance(adapter, ProtocolAdapter)
        assert hasattr(adapter, "adapt_request") and hasattr(adapter, "adapt_stream")


def test_adapter_request_delegates_to_the_pure_builder() -> None:
    """ADAPTER: adapt_request is the canonical→vendor half (delegates to the pure builder)."""
    params = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    assert op.OpenAIAdapter().adapt_request(params) == op.build_openai_request(params)
    gparams = {"model": "gemini-2.0-flash", "messages": [{"role": "user", "content": "hi"}]}
    assert gp.GeminiAdapter().adapt_request(gparams) == gp.build_gemini_request(gparams)


def test_strategy_composes_its_adapter() -> None:
    """STRATEGY composes an ADAPTER (Anthropic is the identity passthrough, so it has none)."""
    assert isinstance(op.OpenAIProvider()._adapter, op.OpenAIAdapter)
    assert isinstance(gp.GeminiProvider()._adapter, gp.GeminiAdapter)


def test_facade_selects_and_hides_the_provider() -> None:
    """FACADE: ModelGateway exposes provider_name/get_client/open_stream, hiding which is used."""
    gw = get_model_gateway("gpt-4o")
    assert isinstance(gw, ModelGateway)
    assert gw.provider_name == "openai"
    assert hasattr(gw, "get_client") and hasattr(gw, "open_stream")
    assert get_model_gateway("claude-opus-4-8").provider_name == "anthropic"
