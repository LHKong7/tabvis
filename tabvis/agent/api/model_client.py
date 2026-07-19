"""Anthropic model client.

Implements the streaming happy path (``query_model_with_streaming``):

* model_client owns the stream-event wrap — for **every** raw part it yields
  ``{'type':'stream_event', 'event': <raw SDK event>, 'ttftMs'?}``;
* at ``content_block_stop`` it builds an AssistantMessage (camelCase envelope: ``requestId``,
  ``uuid``, ``timestamp``; snake inner wire keys) and yields it;
* at ``message_delta`` it ``update_usage``s and **mutates the last message's usage/stop_reason
  in place** (the envelope is a plain dict), then yields refusal / max_tokens / context-window
  ``create_assistant_api_error_message`` (assistant envelopes), never the system sentinel here.

Retries go through ``with_retry`` (RetryError/RetryResult protocol). Not implemented in this
build: non-streaming fallback recovery, idle watchdog, advisor/research, cost accounting, betas,
context-management/output-config params, ant-only paths.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from tabvis.agent.api.empty_usage import empty_usage
from tabvis.agent.api.providers import get_model_gateway
from tabvis.agent.api.errors import (
    API_ERROR_MESSAGE_PREFIX,
    get_assistant_message_from_error,
    get_error_message_if_refusal,
)
from tabvis.agent.api.with_retry import (
    APIUserAbortError,
    CannotRetryError,
    FallbackTriggeredError,
    RetryContext,
    RetryError,
    RetryOptions,
    RetryResult,
    with_retry,
)
from tabvis.tool import Tools
from tabvis.utils.abort import AbortSignal
from tabvis.utils.api import split_sys_prompt_prefix, tool_to_api_schema
from tabvis.utils.context import get_model_max_output_tokens
from tabvis.utils.messages import (
    create_assistant_api_error_message,
    normalize_content_from_api,
    normalize_messages_for_api,
)
from tabvis.utils.model.model import normalize_model_string_for_api
from tabvis.utils.system_prompt_type import SystemPrompt
from tabvis.utils.thinking import DISABLED_THINKING, ThinkingConfig

DEFAULT_MAX_OUTPUT_TOKENS = 8192


def get_max_output_tokens() -> int:
    raw = os.environ.get("TABVIS_MAX_OUTPUT_TOKENS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_TOKENS


DEFAULT_STREAM_IDLE_TIMEOUT_S = 90.0


def _stream_idle_timeout_s() -> float:
    """Max seconds to wait for the NEXT SSE part before treating the stream as stalled.

    A proxy endpoint (GLM/DeepSeek) can hold a 200 stream open under load and stop emitting bytes — a
    silent stall that raises no transport error, so without this the drain blocks up to the full httpx
    read timeout (~600s) per attempt. Override via ``TABVIS_STREAM_IDLE_TIMEOUT`` (seconds)."""
    raw = os.environ.get("TABVIS_STREAM_IDLE_TIMEOUT")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_STREAM_IDLE_TIMEOUT_S


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def get_cache_control(scope: str | None = None, query_source: str | None = None) -> dict[str, Any]:
    cc: dict[str, Any] = {"type": "ephemeral"}
    if scope is not None:
        cc["scope"] = scope
    return cc


def _as_dict(obj: Any) -> dict[str, Any]:
    """Coerce an anthropic SDK pydantic event/usage object to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return dict(obj)


# --------------------------------------------------------------------------------------------
# Usage accounting
# --------------------------------------------------------------------------------------------


def _gt0_or(value: Any, fallback: int) -> int:
    return value if (value is not None and value > 0) else fallback


def _coalesce(value: Any, fallback: Any) -> Any:
    return value if value is not None else fallback


def update_usage(usage: dict[str, Any], part_usage: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a streamed usage delta. ``>0`` guard for input/cache (so message_delta can't zero
    a real value); ``??`` for output_tokens (0 accepted)."""
    if not part_usage:
        return dict(usage)
    p_stu = part_usage.get("server_tool_use") or {}
    u_stu = usage.get("server_tool_use") or {}
    p_cc = part_usage.get("cache_creation") or {}
    u_cc = usage.get("cache_creation") or {}
    return {
        "input_tokens": _gt0_or(part_usage.get("input_tokens"), usage["input_tokens"]),
        "cache_creation_input_tokens": _gt0_or(
            part_usage.get("cache_creation_input_tokens"), usage["cache_creation_input_tokens"]
        ),
        "cache_read_input_tokens": _gt0_or(
            part_usage.get("cache_read_input_tokens"), usage["cache_read_input_tokens"]
        ),
        "output_tokens": _coalesce(part_usage.get("output_tokens"), usage["output_tokens"]),
        "server_tool_use": {
            "web_search_requests": _coalesce(
                p_stu.get("web_search_requests"), u_stu.get("web_search_requests", 0)
            ),
            "web_fetch_requests": _coalesce(
                p_stu.get("web_fetch_requests"), u_stu.get("web_fetch_requests", 0)
            ),
        },
        "service_tier": usage.get("service_tier"),
        "cache_creation": {
            "ephemeral_1h_input_tokens": _coalesce(
                p_cc.get("ephemeral_1h_input_tokens"), u_cc.get("ephemeral_1h_input_tokens", 0)
            ),
            "ephemeral_5m_input_tokens": _coalesce(
                p_cc.get("ephemeral_5m_input_tokens"), u_cc.get("ephemeral_5m_input_tokens", 0)
            ),
        },
        "inference_geo": usage.get("inference_geo"),
        "iterations": _coalesce(part_usage.get("iterations"), usage.get("iterations")),
        "speed": _coalesce(part_usage.get("speed"), usage.get("speed")),
    }


def accumulate_usage(total: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    """Sum token fields across turns; take service_tier/inference_geo/iterations/speed from the latest."""
    t_stu = total.get("server_tool_use") or {}
    m_stu = message.get("server_tool_use") or {}
    t_cc = total.get("cache_creation") or {}
    m_cc = message.get("cache_creation") or {}
    return {
        "input_tokens": total["input_tokens"] + message["input_tokens"],
        "cache_creation_input_tokens": total["cache_creation_input_tokens"]
        + message["cache_creation_input_tokens"],
        "cache_read_input_tokens": total["cache_read_input_tokens"]
        + message["cache_read_input_tokens"],
        "output_tokens": total["output_tokens"] + message["output_tokens"],
        "server_tool_use": {
            "web_search_requests": t_stu.get("web_search_requests", 0)
            + m_stu.get("web_search_requests", 0),
            "web_fetch_requests": t_stu.get("web_fetch_requests", 0)
            + m_stu.get("web_fetch_requests", 0),
        },
        "service_tier": message.get("service_tier"),
        "cache_creation": {
            "ephemeral_1h_input_tokens": t_cc.get("ephemeral_1h_input_tokens", 0)
            + m_cc.get("ephemeral_1h_input_tokens", 0),
            "ephemeral_5m_input_tokens": t_cc.get("ephemeral_5m_input_tokens", 0)
            + m_cc.get("ephemeral_5m_input_tokens", 0),
        },
        "inference_geo": message.get("inference_geo"),
        "iterations": message.get("iterations"),
        "speed": message.get("speed"),
    }


# --------------------------------------------------------------------------------------------
# Message -> API param converters
# --------------------------------------------------------------------------------------------


def _cache_last_block(content: list[dict[str, Any]], query_source: str | None) -> list[dict[str, Any]]:
    out = []
    for i, block in enumerate(content):
        nb = dict(block)
        if i == len(content) - 1 and nb.get("type") not in ("thinking", "redacted_thinking"):
            nb["cache_control"] = get_cache_control(query_source=query_source)
        out.append(nb)
    return out


def user_message_to_message_param(
    message: dict[str, Any],
    add_cache: bool = False,
    enable_prompt_caching: bool = False,
    query_source: str | None = None,
) -> dict[str, Any]:
    content = message["message"]["content"]
    if add_cache:
        if isinstance(content, str):
            block: dict[str, Any] = {"type": "text", "text": content}
            if enable_prompt_caching:
                block["cache_control"] = get_cache_control(query_source=query_source)
            return {"role": "user", "content": [block]}
        if enable_prompt_caching:
            return {"role": "user", "content": _cache_last_block(content, query_source)}
        return {"role": "user", "content": [dict(b) for b in content]}
    # Clone array content to avoid in-place mutation contamination.
    return {"role": "user", "content": list(content) if isinstance(content, list) else content}


def assistant_message_to_message_param(
    message: dict[str, Any],
    add_cache: bool = False,
    enable_prompt_caching: bool = False,
    query_source: str | None = None,
) -> dict[str, Any]:
    content = message["message"]["content"]
    if add_cache:
        if isinstance(content, str):
            block: dict[str, Any] = {"type": "text", "text": content}
            if enable_prompt_caching:
                block["cache_control"] = get_cache_control(query_source=query_source)
            return {"role": "assistant", "content": [block]}
        if enable_prompt_caching:
            return {"role": "assistant", "content": _cache_last_block(content, query_source)}
        return {"role": "assistant", "content": [dict(b) for b in content]}
    return {"role": "assistant", "content": content}


def build_system_prompt_blocks(
    system_prompt: SystemPrompt,
    enable_prompt_caching: bool,
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    opts = options or {}
    blocks = split_sys_prompt_prefix(
        system_prompt, {"skipGlobalCacheForSystemPrompt": opts.get("skipGlobalCacheForSystemPrompt")}
    )
    out: list[dict[str, Any]] = []
    for block in blocks:
        b: dict[str, Any] = {"type": "text", "text": block["text"]}
        if enable_prompt_caching and block.get("cacheScope") is not None:
            b["cache_control"] = get_cache_control(
                scope=block.get("cacheScope"), query_source=opts.get("querySource")
            )
        out.append(b)
    return out


def _messages_to_api_params(
    messages: list[dict[str, Any]],
    tools: Tools,
    enable_prompt_caching: bool,
    query_source: str | None,
) -> list[dict[str, Any]]:
    normalized = normalize_messages_for_api(messages, tools)
    params: list[dict[str, Any]] = []
    n = len(normalized)
    for i, m in enumerate(normalized):
        add_cache = i == n - 1  # cache breakpoint on the last message
        if m["type"] == "user":
            params.append(
                user_message_to_message_param(m, add_cache, enable_prompt_caching, query_source)
            )
        else:
            params.append(
                assistant_message_to_message_param(m, add_cache, enable_prompt_caching, query_source)
            )
    return params


# --------------------------------------------------------------------------------------------
# Options + streaming entry
# --------------------------------------------------------------------------------------------


@dataclass
class Options:
    """Options bag used by the streaming happy path."""

    model: str
    get_tool_permission_context: Callable[[], Awaitable[Any]] | None = None
    is_non_interactive_session: bool = True
    query_source: str | None = None
    agents: list[Any] = field(default_factory=list)
    enable_prompt_caching: bool = False
    fallback_model: str | None = None
    agent_id: str | None = None
    max_output_tokens_override: int | None = None


class _BufferedStream:
    """A fully-drained SDK stream: holds the raw parts in memory and re-exposes ``request_id``.

    Returned by ``operation`` so that stream *consumption* happens inside the ``with_retry``
    boundary (where a mid-stream transport disconnect is retryable). Downstream code iterates this
    exactly like the live SDK stream (``async for part in stream`` + ``stream.request_id``)."""

    def __init__(self, parts: list[Any], request_id: str | None) -> None:
        self._parts = parts
        self.request_id = request_id

    def __aiter__(self) -> AsyncGenerator[Any, None]:
        async def _gen() -> AsyncGenerator[Any, None]:
            for part in self._parts:
                yield part

        return _gen()


async def query_model_with_streaming(
    *,
    messages: list[dict[str, Any]],
    system_prompt: SystemPrompt,
    thinking_config: ThinkingConfig | None,
    tools: Tools,
    signal: AbortSignal | None,
    options: Options,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream a model response. Yields ``stream_event`` wraps, per-block AssistantMessages, and
    recoverable error AssistantMessages / retry-heartbeat system sentinels."""
    enable_caching = options.enable_prompt_caching
    api_messages = _messages_to_api_params(messages, tools, enable_caching, options.query_source)
    system_blocks = build_system_prompt_blocks(
        system_prompt, enable_caching, {"querySource": options.query_source}
    )
    tool_opts = {
        "tools": tools,
        "agents": options.agents,
        "model": options.model,
        "get_tool_permission_context": options.get_tool_permission_context,
    }
    tool_schemas = (
        list(await asyncio.gather(*[tool_to_api_schema(t, tool_opts) for t in tools]))
        if tools
        else []
    )
    max_tokens = options.max_output_tokens_override or get_max_output_tokens()
    # Defensive clamp: never request more than the model's advertised output ceiling. An over-cap
    # TABVIS_MAX_OUTPUT_TOKENS would otherwise be sent verbatim and rejected as a hard, non-retryable
    # 400 (a dead, output-less turn). For an unknown model tabvis's ceiling is 64000.
    _output_upper = get_model_max_output_tokens(options.model)["upperLimit"]
    if max_tokens > _output_upper:
        max_tokens = _output_upper

    def params_from_context(ctx: RetryContext) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": normalize_model_string_for_api(ctx.model),
            "max_tokens": ctx.max_tokens_override or max_tokens,
            "messages": api_messages,
        }
        if system_blocks:
            params["system"] = system_blocks
        if tool_schemas:
            params["tools"] = tool_schemas
        return params

    # The FACADE hides provider selection + client + protocol adaptation behind get_client/open_stream.
    gateway = get_model_gateway(options.model, options.query_source)

    async def get_client() -> Any:
        return await gateway.get_client()

    async def operation(client: Any, _attempt: int, ctx: RetryContext) -> Any:
        params = params_from_context(ctx)
        # The gateway turns these Anthropic request params into an Anthropic-shaped stream — a
        # passthrough for anthropic, an adapter translation for openai/gemini. All below is unchanged.
        stream = await gateway.open_stream(client, params)
        # Drain the stream *inside* the retry boundary. The anthropic SDK opens the HTTP stream
        # lazily — a mid-stream disconnect (httpx.RemoteProtocolError: "peer closed connection
        # without sending complete message body" / incomplete chunked read) is only raised while
        # iterating ``response.aiter_bytes()``, i.e. while consuming this stream, NOT at create()
        # time. By materializing here we let with_retry catch that transient error and retry the
        # whole request rather than letting it propagate out of the headless turn and crash the run.
        # Drain part-by-part with an INACTIVITY watchdog. A proxy that holds the 200 stream open but
        # stops emitting bytes is a silent stall (no transport error) that would otherwise block up to
        # the full httpx read timeout. On idle, raise a retryable transport error so with_retry
        # abandons the stuck stream and retries a fresh request instead of hanging.
        idle_s = _stream_idle_timeout_s()
        stream_iter = stream.__aiter__()
        parts: list[Any] = []
        while True:
            try:
                part = await asyncio.wait_for(stream_iter.__anext__(), timeout=idle_s)
            except StopAsyncIteration:
                break
            except TimeoutError as exc:  # asyncio.wait_for idle timeout (alias of builtin in py3.11+)
                raise httpx.ReadTimeout(
                    f"stream idle for >{idle_s:.0f}s (no SSE part received)"
                ) from exc
            parts.append(part)
        return _BufferedStream(parts, getattr(stream, "request_id", None))

    retry_options = RetryOptions(
        model=options.model,
        thinking_config=thinking_config or DISABLED_THINKING,
        fallback_model=options.fallback_model,
        signal=signal,
        query_source=options.query_source,
    )

    stream: Any = None
    try:
        async for item in with_retry(get_client, operation, retry_options):
            if isinstance(item, RetryError):
                yield item.message  # system api_error sentinel (dropped at SDK boundary)
            elif isinstance(item, RetryResult):
                stream = item.value
                break
    except (APIUserAbortError, asyncio.CancelledError, GeneratorExit):
        # Deliberate abort / cancellation — let it propagate so the turn unwinds cleanly.
        raise
    except (CannotRetryError, FallbackTriggeredError) as error:
        # A non-retryable model error (exhausted retries / triggered fallback / e.g. a GLM 400 or a
        # mid-stream error) would otherwise propagate UNCAUGHT all the way to asyncio.run() and CRASH
        # the headless process, zeroing the run. Convert it to a graceful assistant api-error message
        # and end the turn normally, so any already-written submission/audit.md survives. (Only
        # CannotRetryError carries original_error; FallbackTriggeredError does not — hence getattr.)
        original = getattr(error, "original_error", None) or error
        yield get_assistant_message_from_error(
            original, normalize_model_string_for_api(options.model)
        )
        return
    except Exception as error:  # noqa: BLE001 - last resort: never let a model error crash the run
        yield get_assistant_message_from_error(
            error, normalize_model_string_for_api(options.model)
        )
        return
    if stream is None:
        return

    request_id = getattr(stream, "request_id", None)
    usage = empty_usage()
    partial_message: dict[str, Any] | None = None
    content_blocks: dict[int, dict[str, Any]] = {}
    new_messages: list[dict[str, Any]] = []
    ttft_ms = 0

    async for part in stream:
        ptype = part.type
        if ptype == "message_start":
            partial_message = _as_dict(part.message)
            usage = update_usage(usage, _as_dict(getattr(part.message, "usage", None)))
        elif ptype == "content_block_start":
            blk = _as_dict(part.content_block)
            if blk.get("type") == "tool_use":
                blk["input"] = ""  # accumulate streamed JSON as a string
            content_blocks[part.index] = blk
        elif ptype == "content_block_delta":
            blk = content_blocks.get(part.index)
            if blk is not None:
                delta = part.delta
                dtype = getattr(delta, "type", None)
                if dtype == "text_delta":
                    blk["text"] = blk.get("text", "") + (getattr(delta, "text", "") or "")
                elif dtype == "input_json_delta":
                    blk["input"] = (blk.get("input") or "") + (
                        getattr(delta, "partial_json", "") or ""
                    )
                elif dtype == "thinking_delta":
                    blk["thinking"] = blk.get("thinking", "") + (
                        getattr(delta, "thinking", "") or ""
                    )
                elif dtype == "signature_delta":
                    blk["signature"] = blk.get("signature", "") + (
                        getattr(delta, "signature", "") or ""
                    )
        elif ptype == "content_block_stop":
            blk = content_blocks.pop(part.index, None)
            if blk is not None and partial_message is not None:
                normalized = normalize_content_from_api([blk], tools, options.agent_id)
                m: dict[str, Any] = {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "requestId": request_id,
                    "message": {**partial_message, "content": normalized},
                }
                new_messages.append(m)
                yield m
        elif ptype == "message_delta":
            usage = update_usage(usage, _as_dict(getattr(part, "usage", None)))
            stop_reason = getattr(part.delta, "stop_reason", None)
            if new_messages:
                last = new_messages[-1]
                last["message"]["usage"] = usage  # in-place mutation (plain dict)
                last["message"]["stop_reason"] = stop_reason
            refusal = get_error_message_if_refusal(stop_reason, options.model)
            if refusal:
                yield refusal
            if stop_reason == "max_tokens":
                yield create_assistant_api_error_message(
                    content=(
                        f"{API_ERROR_MESSAGE_PREFIX}: Tabvis's response exceeded the {max_tokens} "
                        "output token maximum. To configure this behavior, set the "
                        "TABVIS_MAX_OUTPUT_TOKENS environment variable."
                    ),
                    api_error="max_output_tokens",
                    error="max_output_tokens",
                )
            elif stop_reason == "model_context_window_exceeded":
                yield create_assistant_api_error_message(
                    content=f"{API_ERROR_MESSAGE_PREFIX}: The model has reached its context window limit.",
                    api_error="max_output_tokens",
                    error="max_output_tokens",
                )
        # message_stop: nothing.

        # For EVERY part: yield the stream_event wrap (model_client owns it).
        event: dict[str, Any] = {"type": "stream_event", "event": part}
        if ptype == "message_start":
            event["ttftMs"] = ttft_ms
        yield event


async def query_with_model(params: dict[str, Any]) -> dict[str, Any]:
    """Non-streaming single-shot model call.

    Builds a single user message from ``userPrompt``, drives :func:`query_model_with_streaming`, and
    accumulates the streamed per-block AssistantMessages into ONE assembled assistant message
    (content blocks concatenated; envelope/usage/stop_reason taken from the final block). No tools
    are offered — the callers (workflow-script generation, etc.) want a plain text/JSON answer — so
    no tool-permission context is needed (``tool_schemas`` is built only when ``tools`` is non-empty).

    ``params`` shape: ``{systemPrompt, userPrompt, signal, options:{model,
    isNonInteractiveSession, querySource, agents, enablePromptCaching, ...}}`` (camelCase wire keys).
    Returns an AssistantMessage dict (``{"type":"assistant","message":{...,"content":[...]}}``);
    extract its text with :func:`tabvis.utils.messages.get_assistant_message_text`.
    """
    from tabvis.utils.messages import create_user_message

    opts = params.get("options") or {}
    options = Options(
        model=opts.get("model"),
        is_non_interactive_session=bool(opts.get("isNonInteractiveSession", True)),
        query_source=opts.get("querySource") or "sdk",
        agents=opts.get("agents") or [],
        enable_prompt_caching=bool(opts.get("enablePromptCaching", False)),
    )

    assembled: dict[str, Any] | None = None
    content: list[dict[str, Any]] = []
    async for event in query_model_with_streaming(
        messages=[create_user_message(content=params.get("userPrompt") or "")],
        system_prompt=params.get("systemPrompt"),
        thinking_config=DISABLED_THINKING,
        tools=[],
        signal=params.get("signal"),
        options=options,
    ):
        if (
            isinstance(event, dict)
            and event.get("type") == "assistant"
            and not event.get("isApiErrorMessage")
        ):
            assembled = event
            content.extend((event.get("message") or {}).get("content") or [])

    if assembled is None:
        return {"type": "assistant", "message": {"role": "assistant", "content": []}}
    return {**assembled, "message": {**assembled["message"], "content": content}}
