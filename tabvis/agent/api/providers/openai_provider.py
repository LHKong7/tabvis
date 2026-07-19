"""OpenAI provider — Chat Completions. Translates Anthropic request params ↔ OpenAI, both ways.

The request builder and the stream translator are module-level pure functions (no SDK, no network),
so they unit-test with hand-built chunk objects. ``OpenAIProvider`` just wires them to
``openai.AsyncOpenAI(...).chat.completions.create(stream=True)``.

Chat Completions is deliberate: it is the most widely implemented surface (OpenAI itself plus every
OpenAI-compatible gateway — vLLM, Groq, Together, local servers), so this one adapter reaches far
beyond OpenAI. Tool calling uses ``tools=[{type:function}]`` + streamed ``tool_calls`` deltas.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from tabvis.agent.api.providers import _events as ev
from tabvis.agent.api.providers import strip_provider_prefix

# --------------------------------------------------------------------------- request: Anthropic → OpenAI


def _system_text(system: Any) -> str:
    """Anthropic system (a string, or a list of ``{type:text,text}`` blocks) → one string."""
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict)).strip()


def _tool_result_to_text(content: Any) -> str:
    """An Anthropic tool_result's content (string | blocks) → a plain string (OpenAI tool role)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        url = f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
        return {"type": "image_url", "image_url": {"url": url}}
    if src.get("type") == "url" and src.get("url"):
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    return None


def _convert_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """One Anthropic message → one or more OpenAI messages (tool_result blocks become tool messages)."""
    role = message.get("role", "user")
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return [{"role": role, "content": content or ""}]

    out: list[dict[str, Any]] = []
    text_bits: list[str] = []
    image_parts: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_bits.append(block.get("text", ""))
        elif btype == "image":
            part = _image_part(block)
            if part:
                image_parts.append(part)
        elif btype == "tool_use":  # assistant asking to call a tool
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                }
            )
        elif btype == "tool_result":  # user returning a tool's output → its own tool message
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _tool_result_to_text(block.get("content")),
                }
            )

    if role == "assistant":
        text = "".join(text_bits)
        msg: dict[str, Any] = {"role": "assistant"}
        # OpenAI wants content=null (not "") when there are tool_calls and no text.
        msg["content"] = text if text or not tool_calls else None
        if tool_calls:
            msg["tool_calls"] = tool_calls
        # Prepend the assistant message before any tool messages that followed in the same block list.
        out.insert(0, msg)
    else:
        # A user message: text (+ images) become a normal user message; tool_results already emitted.
        if image_parts:
            parts: list[dict[str, Any]] = [{"type": "text", "text": "".join(text_bits)}] if text_bits else []
            parts.extend(image_parts)
            out.insert(0, {"role": "user", "content": parts})
        elif text_bits:
            out.insert(0, {"role": "user", "content": "".join(text_bits)})
    return out


def _convert_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Anthropic tool schema ``{name,description,input_schema}`` → OpenAI function tool."""
    return {
        "type": "function",
        "function": {
            "name": tool.get("name"),
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
        },
    }


def build_openai_request(params: dict[str, Any]) -> dict[str, Any]:
    """Anthropic request params → an OpenAI ``chat.completions.create`` kwargs dict."""
    messages: list[dict[str, Any]] = []
    system = _system_text(params.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for m in params.get("messages", []):
        messages.extend(_convert_message(m))

    req: dict[str, Any] = {
        "model": strip_provider_prefix(params.get("model")),
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if params.get("max_tokens"):
        req["max_tokens"] = params["max_tokens"]
    tools = params.get("tools")
    if tools:
        req["tools"] = [_convert_tool(t) for t in tools]
    return req


# --------------------------------------------------------------------------- response: OpenAI → Anthropic

_FINISH_TO_STOP = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "end_turn",
}


def _convert_usage(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    return {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
        "cache_read_input_tokens": cached or 0,
    }


async def translate_openai_stream(
    chunks: AsyncIterator[Any], *, model: str
) -> AsyncIterator[Any]:
    """OpenAI streaming chunks → Anthropic-shaped stream parts (text + tool_use + usage + stop)."""
    started = False
    text_index: int | None = None
    tool_slots: dict[int, int] = {}   # openai tool_call index -> anthropic block index
    open_blocks: set[int] = set()
    next_index = 0
    finish_reason: str | None = None
    final_usage: dict[str, Any] = {}

    async for chunk in chunks:
        if not started:
            yield ev.message_start(message_id=getattr(chunk, "id", None) or "msg_openai", model=model)
            started = True

        usage = getattr(chunk, "usage", None)
        if usage is not None:
            final_usage = _convert_usage(usage)

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

        if delta is None:
            continue

        text = getattr(delta, "content", None)
        if text:
            if text_index is None:
                text_index = next_index
                next_index += 1
                open_blocks.add(text_index)
                yield ev.content_block_start(index=text_index, block={"type": "text", "text": ""})
            yield ev.text_delta(index=text_index, text=text)

        for tc in getattr(delta, "tool_calls", None) or []:
            tc_i = getattr(tc, "index", 0) or 0
            if tc_i not in tool_slots:
                # A tool call starts: close the text block first (Anthropic blocks are ordered).
                if text_index is not None:
                    yield ev.content_block_stop(index=text_index)
                    open_blocks.discard(text_index)
                    text_index = None
                idx = next_index
                next_index += 1
                tool_slots[tc_i] = idx
                open_blocks.add(idx)
                fn = getattr(tc, "function", None)
                yield ev.content_block_start(
                    index=idx,
                    block={
                        "type": "tool_use",
                        "id": getattr(tc, "id", None) or f"call_{tc_i}",
                        "name": getattr(fn, "name", None) or "",
                        "input": "",
                    },
                )
            fn = getattr(tc, "function", None)
            args = getattr(fn, "arguments", None) if fn is not None else None
            if args:
                yield ev.input_json_delta(index=tool_slots[tc_i], partial_json=args)

    for idx in sorted(open_blocks):
        yield ev.content_block_stop(index=idx)
    yield ev.message_delta(stop_reason=_FINISH_TO_STOP.get(finish_reason or "", "end_turn"), usage=final_usage)
    yield ev.message_stop()


# --------------------------------------------------------------------------- Adapter + Strategy


class OpenAIAdapter:
    """ADAPTER: canonical (Anthropic) protocol ↔ OpenAI Chat Completions (bidirectional)."""

    name = "openai"

    def adapt_request(self, params: dict[str, Any]) -> dict[str, Any]:
        return build_openai_request(params)

    def adapt_stream(self, vendor_stream: Any, *, model: str) -> AsyncIterator[Any]:
        return translate_openai_stream(vendor_stream, model=model)


class OpenAIProvider:
    """STRATEGY: reach OpenAI (or any OpenAI-compatible endpoint); convert via :class:`OpenAIAdapter`."""

    name = "openai"

    def __init__(self, *, source: str | None = None) -> None:
        self._source = source
        self._adapter = OpenAIAdapter()

    async def get_client(self) -> Any:
        try:
            from openai import AsyncOpenAI  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "TABVIS_MODEL_PROVIDER=openai (or an OpenAI model was selected), but the 'openai' "
                "package is not installed. Install the optional extra (`uv sync --extra openai`)."
            ) from e
        api_key = os.environ.get("TABVIS_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
        base_url = os.environ.get("TABVIS_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def create_stream(self, client: Any, params: dict[str, Any]) -> AsyncIterator[Any]:
        req = self._adapter.adapt_request(params)             # canonical -> OpenAI request
        stream = await client.chat.completions.create(**req)
        return self._adapter.adapt_stream(stream, model=req["model"])  # OpenAI stream -> canonical
