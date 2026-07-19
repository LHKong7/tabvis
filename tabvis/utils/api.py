"""API schema + system-prompt cache helpers

This module implements the two pieces the runtime spine needs:

* :func:`tool_to_api_schema` — serialize a :class:`~tabvis.tool.Tool` to a ``BetaTool`` dict
  ``{name, description, input_schema, cache_control?, defer_loading?, strict?,
  eager_input_streaming?}``. ``input_schema`` is the tool's pydantic
  ``input_schema.model_json_schema(by_alias=True)`` (the TS tree called ``zodToJsonSchema``).
* :func:`split_sys_prompt_prefix` — split the rendered system prompt into cache-scoped blocks.

Intentionally NOT implemented here (reserved / out of slice scope):

* ``build_system_prompt_blocks`` — reserved for ``services/api/model_client.py``.
* ``get_cache_control`` — it lives in ``modelClient.ts`` (reserved), not ``api.ts``.
* ``normalize_tool_input`` / ``normalize_tool_input_for_api`` / ``log_context_metrics`` /
  ``append_system_context`` / ``prepend_user_context`` — deeper deps (plans, MCP, swarms);
  implemented on demand with their call sites.

GrowthBook / Statsig gates are assumed at their **default** values per the behavior contract
(``docs/SPINE_CONTRACTS.md`` skeleton decisions): swarms disabled is irrelevant for the base
tool pool (none of the 6 base tools carry swarm fields), strict-tools flag defaults off, and
fine-grained tool streaming is off unless ``TABVIS_ENABLE_FINE_GRAINED_TOOL_STREAMING`` is set.

Casing: Python identifiers snake_case; the emitted ``BetaTool`` dict and ``cache_control`` keep
Anthropic wire keys (``input_schema``, ``cache_control``, ``defer_loading``,
``eager_input_streaming``, ``type``, ``scope``, ``ttl``).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy

if TYPE_CHECKING:
    from tabvis.tool import Tool
    from tabvis.utils.system_prompt_type import SystemPrompt

# --- Local constants owned by this slice ---------------------------------------------------
# tabvis/constants/system.py (CLI_SYSPROMPT_PREFIXES) when those modules are implemented, then import
# from there. Mirrors src/constants/prompts.ts and src/constants/system.ts.
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"

_DEFAULT_PREFIX = "You are Tabvis, a browser agent that operates a real web browser to accomplish tasks on the web."
_AGENT_SDK_TABVIS_PRESET_PREFIX = "You are Tabvis, a browser agent running within the agent SDK."
_AGENT_SDK_PREFIX = "You are an agent built on the agent SDK."
CLI_SYSPROMPT_PREFIXES: frozenset[str] = frozenset(
    {_DEFAULT_PREFIX, _AGENT_SDK_TABVIS_PRESET_PREFIX, _AGENT_SDK_PREFIX}
)

CacheScope = Literal["global", "org"]


class CacheControl(TypedDict, total=False):
    """Anthropic ``cache_control`` (wire keys)."""

    type: Literal["ephemeral"]
    scope: Literal["global", "org"]
    ttl: Literal["5m", "1h"]


class BetaToolSchema(TypedDict, total=False):
    """Emitted ``BetaTool`` dict (Anthropic wire keys). Extra beta fields are intentional."""

    name: str
    description: str
    input_schema: dict[str, Any]
    strict: bool
    eager_input_streaming: bool
    defer_loading: bool
    cache_control: CacheControl


class SystemPromptBlock(TypedDict):
    """A cache-scoped system-prompt block (``cacheScope`` kept camelCase per source)."""

    text: str
    cacheScope: CacheScope | None


# --- GrowthBook / provider gates: defaults per behavior contract -------------------------------
# src/utils/model/providers.ts + src/utils/betas.ts once implemented. Defaults assumed for the
# headless skeleton (see docs/SPINE_CONTRACTS.md skeleton decisions).
def _strict_tools_enabled() -> bool:
    """``checkStatsigFeatureGate_CACHED_MAY_BE_STALE('tengu_tool_pear')`` — defaults off."""
    return False


def _model_supports_structured_outputs(model: str) -> bool:
    """``modelSupportsStructuredOutputs`` — first-party only; default conservative (False)."""
    return False


def _is_agent_swarms_enabled() -> bool:
    """``isAgentSwarmsEnabled()`` — defaults off; base tools carry no swarm fields regardless."""
    return False


def _should_use_global_cache_scope() -> bool:
    """``shouldUseGlobalCacheScope()`` — first-party only; default off for the skeleton."""
    return False


def _fine_grained_tool_streaming_enabled() -> bool:
    """``getFeatureValue('tengu_fgts')`` OR ``TABVIS_ENABLE_FINE_GRAINED_TOOL_STREAMING``.

    First-party/foundry-gated in the source; default off here unless the env override is set.
    """
    return is_env_truthy(os.environ.get("TABVIS_ENABLE_FINE_GRAINED_TOOL_STREAMING"))


# Fields filtered from a tool's input schema when swarms are not enabled. Empty for the base
# pool; kept as a structural mirror of SWARM_FIELDS_BY_TOOL.
# tools are implemented (src/utils/api.ts SWARM_FIELDS_BY_TOOL).
_SWARM_FIELDS_BY_TOOL: dict[str, list[str]] = {}


def _filter_swarm_fields_from_schema(
    tool_name: str, schema: dict[str, Any]
) -> dict[str, Any]:
    """Filter swarm-related fields from a tool's input schema (clones, never mutates)."""
    fields_to_remove = _SWARM_FIELDS_BY_TOOL.get(tool_name)
    if not fields_to_remove:
        return schema

    filtered = {**schema}
    props = filtered.get("properties")
    if isinstance(props, dict):
        filtered_props = {**props}
        for fld in fields_to_remove:
            filtered_props.pop(fld, None)
        filtered["properties"] = filtered_props
    return filtered


async def tool_to_api_schema(
    tool: Tool,
    options: dict[str, Any] | None = None,
) -> BetaToolSchema:
    """Serialize ``tool`` to a ``BetaTool`` dict for the Anthropic API.

    ``options`` (all optional) mirrors the TS bag (snake_case):
    ``{get_tool_permission_context, tools, agents, allowed_agent_types, model, defer_loading,
    cache_control}``.

    Unlike the TS source, this slice does NOT use the session-stable ``toolSchemaCache``
    (``getToolSchemaCache``) — the cache exists purely to freeze GrowthBook flips and
    ``tool.prompt()`` drift mid-session, and with the gates pinned to defaults here it is a
    pure pass-through. The per-call schema is computed directly.
    """
    opts = options or {}

    # input_schema: tool's plain JSON Schema if provided, else from the pydantic input model.
    if tool.input_json_schema:
        input_schema: dict[str, Any] = dict(tool.input_json_schema)
    else:
        input_schema = tool.input_schema.model_json_schema(by_alias=True)

    # Filter swarm-related fields when swarms are not enabled.
    if not _is_agent_swarms_enabled():
        input_schema = _filter_swarm_fields_from_schema(tool.name, input_schema)

    description = await tool.prompt(
        {
            "get_tool_permission_context": opts.get("get_tool_permission_context"),
            "tools": opts.get("tools"),
            "agents": opts.get("agents"),
            "allowed_agent_types": opts.get("allowed_agent_types"),
        }
    )

    schema: BetaToolSchema = {
        "name": tool.name,
        "description": description,
        "input_schema": input_schema,
    }

    # strict: feature flag ON + tool.strict + model provided + model supports it.
    model = opts.get("model")
    if (
        _strict_tools_enabled()
        and tool.strict is True
        and model
        and _model_supports_structured_outputs(model)
    ):
        schema["strict"] = True

    # eager_input_streaming (fine-grained tool streaming): first-party gated in source.
    if _fine_grained_tool_streaming_enabled():
        schema["eager_input_streaming"] = True

    # Per-request overlay: defer_loading + cache_control vary by call.
    if opts.get("defer_loading"):
        schema["defer_loading"] = True

    cache_control = opts.get("cache_control")
    if cache_control:
        schema["cache_control"] = cache_control

    # TABVIS_DISABLE_EXPERIMENTAL_BETAS kill switch: strip everything not in the base allowlist.
    if is_env_truthy(os.environ.get("TABVIS_DISABLE_EXPERIMENTAL_BETAS")):
        allowed = {"name", "description", "input_schema", "cache_control"}
        stripped = [k for k in schema if k not in allowed]
        if stripped:
            _log_strip_once(stripped)
            base: BetaToolSchema = {
                "name": schema["name"],
                "description": schema["description"],
                "input_schema": schema["input_schema"],
            }
            if schema.get("cache_control"):
                base["cache_control"] = schema["cache_control"]
            return base

    return schema


_logged_strip = False


def _log_strip_once(stripped: list[str]) -> None:
    global _logged_strip
    if _logged_strip:
        return
    _logged_strip = True
    log_for_debugging(
        f"[betas] Stripped from tool schemas: [{', '.join(stripped)}] "
        "(TABVIS_DISABLE_EXPERIMENTAL_BETAS=1)"
    )


def split_sys_prompt_prefix(
    system_prompt: SystemPrompt,
    options: dict[str, Any] | None = None,
) -> list[SystemPromptBlock]:
    """Split system prompt blocks by content type for API matching + cache control.

    Three modes (gated by :func:`_should_use_global_cache_scope` and
    ``options['skip_global_cache_for_system_prompt']``):

    1. MCP tools present (skip global) → up to 3 org-scoped blocks.
    2. Global cache + boundary marker present → up to 4 blocks (static→global, dynamic→null).
    3. Default (3P, or boundary missing) → up to 3 org-scoped blocks.
    """
    opts = options or {}
    use_global_cache_feature = _should_use_global_cache_scope()

    if use_global_cache_feature and opts.get("skip_global_cache_for_system_prompt"):
        first_party_header: str | None = None
        system_prompt_prefix: str | None = None
        rest: list[str] = []

        for prompt in system_prompt:
            if not prompt:
                continue
            if prompt == SYSTEM_PROMPT_DYNAMIC_BOUNDARY:
                continue
            if prompt.startswith("x-tabvis-attribution-header"):
                first_party_header = prompt
            elif prompt in CLI_SYSPROMPT_PREFIXES:
                system_prompt_prefix = prompt
            else:
                rest.append(prompt)

        result: list[SystemPromptBlock] = []
        if first_party_header:
            result.append({"text": first_party_header, "cacheScope": None})
        if system_prompt_prefix:
            result.append({"text": system_prompt_prefix, "cacheScope": "org"})
        rest_joined = "\n\n".join(rest)
        if rest_joined:
            result.append({"text": rest_joined, "cacheScope": "org"})
        return result

    if use_global_cache_feature:
        boundary_index = next(
            (i for i, s in enumerate(system_prompt) if s == SYSTEM_PROMPT_DYNAMIC_BOUNDARY),
            -1,
        )
        if boundary_index != -1:
            first_party_header = None
            system_prompt_prefix = None
            static_blocks: list[str] = []
            dynamic_blocks: list[str] = []

            for i, block in enumerate(system_prompt):
                if not block or block == SYSTEM_PROMPT_DYNAMIC_BOUNDARY:
                    continue
                if block.startswith("x-tabvis-attribution-header"):
                    first_party_header = block
                elif block in CLI_SYSPROMPT_PREFIXES:
                    system_prompt_prefix = block
                elif i < boundary_index:
                    static_blocks.append(block)
                else:
                    dynamic_blocks.append(block)

            result = []
            if first_party_header:
                result.append({"text": first_party_header, "cacheScope": None})
            if system_prompt_prefix:
                result.append({"text": system_prompt_prefix, "cacheScope": None})
            static_joined = "\n\n".join(static_blocks)
            if static_joined:
                result.append({"text": static_joined, "cacheScope": "global"})
            dynamic_joined = "\n\n".join(dynamic_blocks)
            if dynamic_joined:
                result.append({"text": dynamic_joined, "cacheScope": None})

            return result
        else:
            pass

    first_party_header = None
    system_prompt_prefix = None
    rest = []

    for block in system_prompt:
        if not block:
            continue
        if block.startswith("x-tabvis-attribution-header"):
            first_party_header = block
        elif block in CLI_SYSPROMPT_PREFIXES:
            system_prompt_prefix = block
        else:
            rest.append(block)

    result = []
    if first_party_header:
        result.append({"text": first_party_header, "cacheScope": None})
    if system_prompt_prefix:
        result.append({"text": system_prompt_prefix, "cacheScope": "org"})
    rest_joined = "\n\n".join(rest)
    if rest_joined:
        result.append({"text": rest_joined, "cacheScope": "org"})
    return result
