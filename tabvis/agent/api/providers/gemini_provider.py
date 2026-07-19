"""Gemini provider — the ``google-genai`` SDK. Translates Anthropic request params ↔ Gemini.

As with the OpenAI provider, the request builder and stream translator are pure module functions
(SDK-free, unit-testable). ``GeminiProvider`` wires them to
``google.genai`` ``client.aio.models.generate_content_stream``.

Two Gemini-isms the translation handles:
- roles are only ``user`` / ``model`` (assistant → ``model``); a tool result is a ``user`` content
  carrying a ``function_response`` part.
- a ``function_response`` needs the function *name*, but an Anthropic ``tool_result`` only carries
  the ``tool_use_id`` — so we pre-map id → name from the assistant ``tool_use`` blocks.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from tabvis.agent.api.providers import _events as ev
from tabvis.agent.api.providers import strip_provider_prefix

# --------------------------------------------------------------------------- request: Anthropic → Gemini


def _tool_name_by_id(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Map every ``tool_use`` id → its name, so a later ``tool_result`` can name its response."""
    names: dict[str, str] = {}
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    names[b["id"]] = b.get("name", "")
    return names


def _tool_result_response(content: Any) -> dict[str, Any]:
    """An Anthropic tool_result content → Gemini ``function_response.response`` (a JSON object)."""
    if isinstance(content, str):
        return {"result": content}
    if isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
        return {"result": text}
    return {"result": "" if content is None else content}


def _inline_data(block: dict[str, Any]) -> dict[str, Any] | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return {"inline_data": {"mime_type": src.get("media_type", "image/png"), "data": src["data"]}}
    return None


def _convert_message(message: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    """One Anthropic message → Gemini ``contents`` entries (usually one; role user|model)."""
    role = "model" if message.get("role") == "assistant" else "user"
    content = message.get("content")
    if isinstance(content, str):
        return [{"role": role, "parts": [{"text": content}]}]
    if not isinstance(content, list):
        return [{"role": role, "parts": [{"text": str(content or "")}]}]

    parts: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"text": block.get("text", "")})
        elif btype == "image":
            part = _inline_data(block)
            if part:
                parts.append(part)
        elif btype == "tool_use":
            parts.append(
                {"function_call": {"name": block.get("name"), "args": block.get("input") or {}}}
            )
        elif btype == "tool_result":
            parts.append(
                {
                    "function_response": {
                        "name": names.get(block.get("tool_use_id", ""), ""),
                        "response": _tool_result_response(block.get("content")),
                    }
                }
            )
    return [{"role": role, "parts": parts}] if parts else []


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic tool schemas → a single Gemini ``tools`` entry of ``function_declarations``."""
    decls = [
        {
            "name": t.get("name"),
            "description": t.get("description", ""),
            "parameters": _clean_schema(t.get("input_schema") or {"type": "object", "properties": {}}),
        }
        for t in tools
    ]
    return [{"function_declarations": decls}]


def _clean_schema(schema: Any) -> Any:
    """Drop JSON-schema keys Gemini's function parameters reject (``$schema``, ``additionalProperties``)."""
    if isinstance(schema, dict):
        return {
            k: _clean_schema(v)
            for k, v in schema.items()
            if k not in ("$schema", "additionalProperties")
        }
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def build_gemini_request(params: dict[str, Any]) -> dict[str, Any]:
    """Anthropic request params → ``{model, contents, config}`` for ``generate_content_stream``."""
    messages = params.get("messages", [])
    names = _tool_name_by_id(messages)
    contents: list[dict[str, Any]] = []
    for m in messages:
        contents.extend(_convert_message(m, names))

    config: dict[str, Any] = {}
    system = params.get("system")
    sys_text = (
        system
        if isinstance(system, str)
        else "\n\n".join(b.get("text", "") for b in (system or []) if isinstance(b, dict)).strip()
    )
    if sys_text:
        config["system_instruction"] = sys_text
    if params.get("max_tokens"):
        config["max_output_tokens"] = params["max_tokens"]
    if params.get("tools"):
        config["tools"] = _convert_tools(params["tools"])

    return {"model": strip_provider_prefix(params.get("model")), "contents": contents, "config": config}


# --------------------------------------------------------------------------- response: Gemini → Anthropic

_GEMINI_FINISH_TO_STOP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "end_turn",
    "RECITATION": "end_turn",
}


def _convert_usage(um: Any) -> dict[str, Any]:
    if um is None:
        return {}
    return {
        "input_tokens": getattr(um, "prompt_token_count", 0) or 0,
        "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
        "cache_read_input_tokens": getattr(um, "cached_content_token_count", 0) or 0,
    }


async def translate_gemini_stream(chunks: AsyncIterator[Any], *, model: str) -> AsyncIterator[Any]:
    """Gemini streaming chunks → Anthropic-shaped stream parts (text + tool_use + usage + stop)."""
    started = False
    text_index: int | None = None
    open_blocks: set[int] = set()
    next_index = 0
    had_tool = False
    finish_reason: str | None = None
    final_usage: dict[str, Any] = {}

    async for chunk in chunks:
        if not started:
            yield ev.message_start(message_id="msg_gemini", model=model)
            started = True

        um = getattr(chunk, "usage_metadata", None)
        if um is not None:
            final_usage = _convert_usage(um)

        candidates = getattr(chunk, "candidates", None) or []
        if not candidates:
            continue
        cand = candidates[0]
        if getattr(cand, "finish_reason", None):
            finish_reason = str(cand.finish_reason)

        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or [] if content is not None else []
        for part in parts:
            text = getattr(part, "text", None)
            fc = getattr(part, "function_call", None)
            if text:
                if text_index is None:
                    text_index = next_index
                    next_index += 1
                    open_blocks.add(text_index)
                    yield ev.content_block_start(index=text_index, block={"type": "text", "text": ""})
                yield ev.text_delta(index=text_index, text=text)
            elif fc is not None:
                # A tool call: close any open text block, then emit the whole call (Gemini gives
                # function args as one object, not a stream) as start + a single input_json_delta.
                if text_index is not None:
                    yield ev.content_block_stop(index=text_index)
                    open_blocks.discard(text_index)
                    text_index = None
                had_tool = True
                idx = next_index
                next_index += 1
                open_blocks.add(idx)
                name = getattr(fc, "name", None) or ""
                args = getattr(fc, "args", None) or {}
                yield ev.content_block_start(
                    index=idx, block={"type": "tool_use", "id": f"call_{idx}", "name": name, "input": ""}
                )
                yield ev.input_json_delta(index=idx, partial_json=json.dumps(_as_plain(args)))

    for idx in sorted(open_blocks):
        yield ev.content_block_stop(index=idx)
    stop = "tool_use" if had_tool else _GEMINI_FINISH_TO_STOP.get(finish_reason or "", "end_turn")
    yield ev.message_delta(stop_reason=stop, usage=final_usage)
    yield ev.message_stop()


def _as_plain(args: Any) -> Any:
    """Coerce Gemini's args (a dict, or a proto Map/Struct) into JSON-serializable data."""
    if isinstance(args, dict):
        return {k: _as_plain(v) for k, v in args.items()}
    if isinstance(args, (list, tuple)):
        return [_as_plain(v) for v in args]
    try:
        json.dumps(args)
        return args
    except (TypeError, ValueError):
        return dict(args) if hasattr(args, "keys") else str(args)


# --------------------------------------------------------------------------- Adapter + Strategy


class GeminiAdapter:
    """ADAPTER: canonical (Anthropic) protocol ↔ Gemini ``google-genai`` (bidirectional)."""

    name = "gemini"

    def adapt_request(self, params: dict[str, Any]) -> dict[str, Any]:
        return build_gemini_request(params)

    def adapt_stream(self, vendor_stream: Any, *, model: str) -> AsyncIterator[Any]:
        return translate_gemini_stream(vendor_stream, model=model)


class GeminiProvider:
    """STRATEGY: reach Gemini via google-genai; convert via :class:`GeminiAdapter`."""

    name = "gemini"

    def __init__(self, *, source: str | None = None) -> None:
        self._source = source
        self._adapter = GeminiAdapter()

    async def get_client(self) -> Any:
        try:
            from google import genai  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "TABVIS_MODEL_PROVIDER=gemini (or a Gemini model was selected), but the 'google-genai' "
                "package is not installed. Install the optional extra (`uv sync --extra gemini`)."
            ) from e
        api_key = (
            os.environ.get("TABVIS_GEMINI_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        base_url = os.environ.get("TABVIS_GEMINI_BASE_URL")
        http_options = {"base_url": base_url} if base_url else None
        return genai.Client(api_key=api_key, http_options=http_options)

    async def create_stream(self, client: Any, params: dict[str, Any]) -> AsyncIterator[Any]:
        req = self._adapter.adapt_request(params)             # canonical -> Gemini request
        stream = await client.aio.models.generate_content_stream(
            model=req["model"], contents=req["contents"], config=req["config"]
        )
        return self._adapter.adapt_stream(stream, model=req["model"])  # Gemini stream -> canonical
