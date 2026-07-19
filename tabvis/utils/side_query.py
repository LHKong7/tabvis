"""Lightweight API wrapper for "side queries"

Use :func:`side_query` instead of a direct ``client.beta.messages.create()`` for one-shot calls
outside the main conversation loop (permission explainer, session search, model validation, memory
relevance). It handles:
- Fingerprint computation for access-token validation.
- Attribution-header injection (kept in its own ``TextBlockParam`` block).
- The CLI system-prompt prefix.
- Proper betas for the model + the structured-outputs beta when an ``output_format`` is supplied.
- API metadata + model-string normalization (strips ``[1m]`` suffix for the API).
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from tabvis.bootstrap.state import (
    get_last_api_completion_timestamp,
    get_session_id,
    set_last_api_completion_timestamp,
)
from tabvis.bootstrap_macro import MACRO
from tabvis.constants.system import getAttributionHeader, getCLISyspromptPrefix
from tabvis.agent.api.client import get_provider_client
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.fingerprint import compute_fingerprint
from tabvis.utils.json import safe_parse_json
from tabvis.utils.model.model import get_canonical_name, normalize_model_string_for_api
from tabvis.utils.model.providers import get_api_provider
from tabvis.utils.slow_operations import json_stringify


def _model_supports_structured_outputs(model: str) -> bool:
    """Whether the model supports the structured-outputs beta (firstParty/foundry only)."""
    canonical = get_canonical_name(model)
    provider = get_api_provider()
    if provider != "firstParty" and provider != "foundry":
        return False
    return (
        "claude-sonnet-4-6" in canonical
        or "claude-sonnet-4-5" in canonical
        or "claude-opus-4-1" in canonical
        or "claude-opus-4-5" in canonical
        or "claude-opus-4-6" in canonical
        or "claude-haiku-4-5" in canonical
    )

# The structured-outputs beta header (inlined from the removed constants/betas.py).
STRUCTURED_OUTPUTS_BETA_HEADER = "structured-outputs-2025-12-15"


# Default budgets (mirror the TS option defaults).
_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_MAX_RETRIES = 2


class SideQueryOptions(TypedDict, total=False):
    """Options for :func:`side_query`."""

    model: str
    system: str | list[dict[str, Any]] | None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    tool_choice: dict[str, Any] | None
    output_format: dict[str, Any] | None
    max_tokens: int | None
    maxRetries: int | None
    signal: Any
    skipSystemPromptPrefix: bool | None
    temperature: float | None
    thinking: int | bool | None
    stop_sequences: list[str] | None
    querySource: str


def _extract_first_user_message_text(messages: list[dict[str, Any]]) -> str:
    """Text of the first user message for fingerprinting."""
    first_user_message = next((m for m in messages if m.get("role") == "user"), None)
    if not first_user_message:
        return ""

    content = first_user_message.get("content")
    if isinstance(content, str):
        return content

    # Array of content blocks - find first text block
    if isinstance(content, list):
        text_block = next(
            (b for b in content if isinstance(b, dict) and b.get("type") == "text"),
            None,
        )
        if text_block is not None and text_block.get("type") == "text":
            return text_block.get("text", "")
    return ""


def get_api_metadata() -> dict[str, Any]:
    """Return the api metadata.

    Lives here as a faithful local helper because the TS source attaches the same metadata to every
    request. ``TABVIS_EXTRA_METADATA`` (a JSON object) is merged into the ``user_id`` payload.
    """
    extra: dict[str, Any] = {}
    extra_str = os.environ.get("TABVIS_EXTRA_METADATA")
    if extra_str:
        parsed = safe_parse_json(extra_str, False)
        if parsed is not None and isinstance(parsed, dict):
            extra = parsed
        else:
            log_for_debugging(
                f"TABVIS_EXTRA_METADATA env var must be a JSON object, but was given {extra_str}",
                {"level": "error"},
            )

    # Lazy import: utils.user pulls config/secure-storage chains we don't want at module load.
    from tabvis.utils.user import _get_or_create_user_id

    return {
        "user_id": json_stringify(
            {
                **extra,
                "device_id": _get_or_create_user_id(),
                "session_id": get_session_id(),
            }
        ),
    }


async def side_query(opts: SideQueryOptions) -> Any:
    """One-shot ``beta.messages.create`` with attribution + betas."""
    model = opts["model"]
    system = opts.get("system")
    messages = opts["messages"]
    tools = opts.get("tools")
    tool_choice = opts.get("tool_choice")
    output_format = opts.get("output_format")
    max_tokens = opts.get("max_tokens")
    if max_tokens is None:
        max_tokens = _DEFAULT_MAX_TOKENS
    max_retries = opts.get("maxRetries")
    if max_retries is None:
        max_retries = _DEFAULT_MAX_RETRIES
    signal = opts.get("signal")
    skip_system_prompt_prefix = opts.get("skipSystemPromptPrefix")
    temperature = opts.get("temperature")
    thinking = opts.get("thinking")
    stop_sequences = opts.get("stop_sequences")

    client = await get_provider_client(
        max_retries=max_retries,
        model=model,
        source="side_query",
    )
    betas: list[str] = []
    # Add structured-outputs beta if using output_format and provider supports it
    if (
        output_format
        and _model_supports_structured_outputs(model)
        and STRUCTURED_OUTPUTS_BETA_HEADER not in betas
    ):
        betas.append(STRUCTURED_OUTPUTS_BETA_HEADER)

    # Extract first user message text for fingerprint
    message_text = _extract_first_user_message_text(messages)

    # Compute fingerprint for access token attribution
    fingerprint = compute_fingerprint(message_text, MACRO.VERSION)
    attribution_header = getAttributionHeader(fingerprint)

    # Build system as array to keep attribution header in its own block
    # (prevents server-side parsing from including system content in cc_entrypoint)
    system_blocks: list[dict[str, Any]] = []
    if attribution_header:
        system_blocks.append({"type": "text", "text": attribution_header})
    if not skip_system_prompt_prefix:
        system_blocks.append(
            {
                "type": "text",
                "text": getCLISyspromptPrefix(
                    {"isNonInteractive": False, "hasAppendSystemPrompt": False}
                ),
            }
        )
    if isinstance(system, list):
        system_blocks.extend(system)
    elif system:
        system_blocks.append({"type": "text", "text": system})

    thinking_config: dict[str, Any] | None = None
    if thinking is False:
        thinking_config = {"type": "disabled"}
    elif thinking is not None:
        thinking_config = {
            "type": "enabled",
            "budget_tokens": min(thinking, max_tokens - 1),
        }

    normalized_model = normalize_model_string_for_api(model)
    start = _now_ms()

    params: dict[str, Any] = {
        "model": normalized_model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
        "metadata": get_api_metadata(),
    }
    if tools:
        params["tools"] = tools
    if tool_choice:
        params["tool_choice"] = tool_choice
    if output_format:
        params["output_config"] = {"format": output_format}
    if temperature is not None:
        params["temperature"] = temperature
    if stop_sequences:
        params["stop_sequences"] = stop_sequences
    if thinking_config:
        params["thinking"] = thinking_config
    if len(betas) > 0:
        params["betas"] = betas

    response = await _create_message(client, params, signal)

    request_id = getattr(response, "_request_id", None) or None
    now = _now_ms()
    last_completion = get_last_api_completion_timestamp()
    usage = response.usage
    set_last_api_completion_timestamp(now)

    return response


async def _create_message(client: Any, params: dict[str, Any], signal: Any) -> Any:
    """Issue the ``beta.messages.create`` call.

    The TS SDK takes a second ``{ signal }`` options object; the anthropic Python SDK has no such
    parameter, so cancellation is driven by the caller's :class:`AbortSignal` externally. ``signal``
    is accepted for call-surface parity and otherwise unused.
    """
    _ = signal
    return await client.beta.messages.create(**params)


def _now_ms() -> int:
    """Current epoch in milliseconds (``Date.now()``)."""
    import time

    return int(time.time() * 1000)
