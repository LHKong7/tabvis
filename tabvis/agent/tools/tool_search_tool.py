"""ToolSearch tool — search and load deferred tool schemas on demand.

The model uses ``ToolSearch`` to load the *deferred* tool registry on demand: tools whose
schemas are withheld from the initial prompt (MCP tools and ``shouldDefer`` tools) appear by
name only, and this tool fetches their full schemas. A query is either:

* ``"select:A,B,C"`` — direct, comma-separated multi-select by exact tool name, or
* free-text keywords (optionally ``+required`` terms) ranked over tool names + descriptions.

It operates over ``context.options.tools`` (the live tool pool), filtering to the deferred
subset via :func:`is_deferred_tool`. The result block uses Anthropic ``tool_reference`` content
blocks — the wire signal that loads each matched tool's schema for subsequent turns.

Implementation notes:

* Input schema: ``query``, optional ``max_results`` (default 5) — see :class:`ToolSearchInput`.
* The keyword scorer (:func:`search_tools_with_keywords`) does an exact-name fast path, ``mcp__``
  prefix matching, required/optional term partition, CamelCase/MCP name parsing, per-term weights,
  word-boundary description/hint matching, and a stable score sort.
* Ranking is deterministic substring / word-boundary scoring, not fuzzy distance — this keeps
  match ordering predictable and reproducible.
* The description cache (memoized lookup + invalidation) is an optimization with no observable
  behavior change; it is a module-level dict keyed by tool name and invalidated when the
  deferred-tool set changes.
* The output dict and the ``tool_result`` block use fixed wire keys (``matches``/``query``/
  ``total_deferred_tools``/``pending_mcp_servers``; ``tool_use_id``/``type``/``content``/
  ``tool_name``) consumed downstream by the SDK.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import (
    Tool,
    ToolResult,
    ToolUseContext,
    find_tool_by_name,
    get_empty_tool_permission_context,
)
from tabvis.utils.debug import log_for_debugging

# ---------------------------------------------------------------------------
# Constants + prompt text
# ---------------------------------------------------------------------------

TOOL_SEARCH_TOOL_NAME = "ToolSearch"

# The Task-agent tool name, referenced only by an unreachable branch in is_deferred_tool below;
# kept local rather than importing a separate agent-tool module for one constant.
AGENT_TOOL_NAME = "Task"

_PROMPT_HEAD = "Fetches full schema definitions for deferred tools so they can be called.\n\n"

_PROMPT_TAIL = (
    " Until fetched, only the name is known — there is no parameter schema, so the tool cannot "
    "be invoked. This tool takes a query, matches it against the deferred tool list, and returns "
    "the matched tools' complete JSONSchema definitions inside a <functions> block. Once a tool's "
    "schema appears in that result, it is callable exactly like any tool defined at the top of "
    "the prompt.\n\n"
    'Result format: each matched tool appears as one <function>{"description": "...", '
    '"name": "...", "parameters": {...}}</function> line inside the <functions> block — the same '
    "encoding as the tool list at the top of this prompt.\n\n"
    "Query forms:\n"
    '- "select:Read,Edit,Grep" — fetch these exact tools by name\n'
    '- "notebook jupyter" — keyword search, up to max_results best matches\n'
    '- "+slack send" — require "slack" in the name, rank by remaining terms'
)


def _get_tool_location_hint() -> str:
    """Compute the tool-location hint for the deferred-tools prompt.

    The delta-enabled gate is disabled in this build, so the hint always uses the
    ``<available-deferred-tools>`` wording.
    """

    delta_enabled = False
    if delta_enabled:
        return "Deferred tools appear by name in <system-reminder> messages."
    return "Deferred tools appear by name in <available-deferred-tools> messages."


def get_prompt() -> str:
    """Assemble the tool's prompt text — head + location hint + tail."""
    return _PROMPT_HEAD + _get_tool_location_hint() + _PROMPT_TAIL


def is_deferred_tool(tool: Tool) -> bool:
    """Whether ``tool`` is only loadable via ToolSearch (i.e. deferred from the initial prompt).

    A tool is deferred iff:

    * NOT ``always_load`` (checked first — MCP tools can opt out via this flag),
    * it is an MCP tool (``is_mcp``), OR
    * it is not ToolSearch itself and has ``should_defer``.
    """
    if tool.always_load is True:
        return False
    if tool.is_mcp is True:
        return True
    if tool.name == TOOL_SEARCH_TOOL_NAME:
        return False
    return tool.should_defer is True


# ---------------------------------------------------------------------------
# Input / output schemas
# ---------------------------------------------------------------------------


class ToolSearchInput(BaseModel):
    """Validated input for :data:`tool_search_tool`."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        description=(
            'Query to find deferred tools. Use "select:<tool_name>" for direct selection, or '
            "keywords to search."
        ),
    )
    max_results: float = Field(
        default=5,
        description="Maximum number of results to return (default: 5)",
    )


# ---------------------------------------------------------------------------
# Regex escaping / description cache
# ---------------------------------------------------------------------------

_ESCAPE_REGEXP_RE = re.compile(r"[.*+?^${}()|[\]\\]")


def _escape_reg_exp(value: str) -> str:
    """Escape regex metacharacters in ``value`` for use inside a compiled pattern."""
    return _ESCAPE_REGEXP_RE.sub(lambda m: "\\" + m.group(0), value)


# Memoized tool description cache, invalidated whenever the deferred-tool set changes.
_description_cache: dict[str, str] = {}
_cached_deferred_tool_names: str | None = None


def _empty_permission_context_options(tools: Any) -> dict[str, Any]:
    """The options bag passed to ``tool.prompt`` when scoring descriptions."""
    return {
        "getToolPermissionContext": _async_empty_permission_context,
        "tools": tools,
        "agents": [],
    }


async def _async_empty_permission_context() -> Any:
    return get_empty_tool_permission_context()


async def _get_tool_description_memoized(tool_name: str, tools: Any) -> str:
    """Memoized ``tool.prompt(...)`` lookup for keyword scoring."""
    cached = _description_cache.get(tool_name)
    if cached is not None:
        return cached
    tool = find_tool_by_name(tools, tool_name)
    if tool is None:
        _description_cache[tool_name] = ""
        return ""
    try:
        description = await tool.prompt(_empty_permission_context_options(tools))
    except Exception:  # noqa: BLE001 - a tool's prompt() must never break search
        description = ""
    description = description or ""
    _description_cache[tool_name] = description
    return description


def _get_deferred_tools_cache_key(deferred_tools: Any) -> str:
    return ",".join(sorted(t.name for t in deferred_tools))


def _maybe_invalidate_cache(deferred_tools: Any) -> None:
    global _cached_deferred_tool_names
    current_key = _get_deferred_tools_cache_key(deferred_tools)
    if _cached_deferred_tool_names != current_key:
        log_for_debugging("ToolSearchTool: cache invalidated - deferred tools changed")
        _description_cache.clear()
        _cached_deferred_tool_names = current_key


def clear_tool_search_description_cache() -> None:
    """Reset the description memo and its cache key."""
    global _cached_deferred_tool_names
    _description_cache.clear()
    _cached_deferred_tool_names = None


# ---------------------------------------------------------------------------
# Name parsing + scoring
# ---------------------------------------------------------------------------

_CAMEL_BOUNDARY_RE = re.compile(r"([a-z])([A-Z])")
_WHITESPACE_RE = re.compile(r"\s+")


def _parse_tool_name(name: str) -> dict[str, Any]:
    """Split a tool name into searchable parts.

    Handles MCP tools (``mcp__server__action``) and regular CamelCase / underscore names.
    """
    if name.startswith("mcp__"):
        without_prefix = re.sub(r"^mcp__", "", name).lower()
        parts: list[str] = []
        for chunk in without_prefix.split("__"):
            parts.extend(chunk.split("_"))
        return {
            "parts": [p for p in parts if p],
            "full": without_prefix.replace("__", " ").replace("_", " "),
            "isMcp": True,
        }

    spaced = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", name).replace("_", " ").lower()
    parts = [p for p in _WHITESPACE_RE.split(spaced) if p]
    return {"parts": parts, "full": " ".join(parts), "isMcp": False}


def _compile_term_patterns(terms: list[str]) -> dict[str, re.Pattern[str]]:
    """Compile a word-boundary regex per unique term."""
    patterns: dict[str, re.Pattern[str]] = {}
    for term in terms:
        if term not in patterns:
            patterns[term] = re.compile(r"\b" + _escape_reg_exp(term) + r"\b")
    return patterns


async def search_tools_with_keywords(
    query: str,
    deferred_tools: Any,
    tools: Any,
    max_results: int,
) -> list[str]:
    """Keyword search over deferred tool names + descriptions, returning ranked tool names."""
    query_lower = query.lower().strip()

    # Fast path: exact tool-name match (deferred first, then the full pool — selecting an
    # already-loaded tool is a harmless no-op).
    exact_match = next(
        (t for t in deferred_tools if t.name.lower() == query_lower),
        None,
    ) or next((t for t in tools if t.name.lower() == query_lower), None)
    if exact_match is not None:
        return [exact_match.name]

    # mcp__server prefix match.
    if query_lower.startswith("mcp__") and len(query_lower) > 5:
        prefix_matches = [
            t.name for t in deferred_tools if t.name.lower().startswith(query_lower)
        ][:max_results]
        if prefix_matches:
            return prefix_matches

    query_terms = [t for t in _WHITESPACE_RE.split(query_lower) if t]

    required_terms: list[str] = []
    optional_terms: list[str] = []
    for term in query_terms:
        if term.startswith("+") and len(term) > 1:
            required_terms.append(term[1:])
        else:
            optional_terms.append(term)

    all_scoring_terms = (
        [*required_terms, *optional_terms] if required_terms else query_terms
    )
    term_patterns = _compile_term_patterns(all_scoring_terms)

    # Pre-filter to tools matching ALL required terms (name parts or description/hint).
    candidate_tools = list(deferred_tools)
    if required_terms:
        filtered: list[Tool] = []
        for tool in deferred_tools:
            parsed = _parse_tool_name(tool.name)
            description = await _get_tool_description_memoized(tool.name, tools)
            desc_normalized = description.lower()
            hint_normalized = (tool.search_hint or "").lower()
            parts = parsed["parts"]

            def _matches(term: str, parts: list[str] = parts,
                         desc: str = desc_normalized, hint: str = hint_normalized) -> bool:
                pattern = term_patterns[term]
                return (
                    term in parts
                    or any(term in part for part in parts)
                    or bool(pattern.search(desc))
                    or (bool(hint) and bool(pattern.search(hint)))
                )

            if all(_matches(term) for term in required_terms):
                filtered.append(tool)
        candidate_tools = filtered

    scored: list[dict[str, Any]] = []
    for tool in candidate_tools:
        parsed = _parse_tool_name(tool.name)
        description = await _get_tool_description_memoized(tool.name, tools)
        desc_normalized = description.lower()
        hint_normalized = (tool.search_hint or "").lower()
        parts = parsed["parts"]
        is_mcp = parsed["isMcp"]
        full = parsed["full"]

        score = 0
        for term in all_scoring_terms:
            pattern = term_patterns[term]

            if term in parts:
                score += 12 if is_mcp else 10
            elif any(term in part for part in parts):
                score += 6 if is_mcp else 5

            if term in full and score == 0:
                score += 3

            if hint_normalized and pattern.search(hint_normalized):
                score += 4

            if pattern.search(desc_normalized):
                score += 2

        scored.append({"name": tool.name, "score": score})

    # Stable sort by descending score (Python sort is stable, matching JS Array.sort here).
    ranked = [item for item in scored if item["score"] > 0]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return [item["name"] for item in ranked[:max_results]]


# ---------------------------------------------------------------------------
# result builder
# ---------------------------------------------------------------------------


def _build_search_result(
    matches: list[str],
    query: str,
    total_deferred_tools: int,
    pending_mcp_servers: list[str] | None = None,
) -> ToolResult[dict[str, Any]]:
    """Assemble the result dict with its fixed wire keys."""
    data: dict[str, Any] = {
        "matches": matches,
        "query": query,
        "total_deferred_tools": total_deferred_tools,
    }
    if pending_mcp_servers:
        data["pending_mcp_servers"] = pending_mcp_servers
    return ToolResult(data=data)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

_SELECT_RE = re.compile(r"^select:(.+)$", re.IGNORECASE)


class ToolSearchTool(Tool):
    """``ToolSearch`` — search the deferred tool registry and load matching schemas."""

    name = TOOL_SEARCH_TOOL_NAME
    input_schema = ToolSearchInput
    max_result_size_chars = 100_000

    def is_enabled(self) -> bool:
        # The tool registry decides enablement (this tool only runs when registered), so this
        # always reports enabled.
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return True

    def is_read_only(self, input: Any) -> bool:
        return True

    def user_facing_name(self, input: Any | None = None) -> str:
        return ""

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return get_prompt()

    async def prompt(self, options: dict[str, Any]) -> str:
        return get_prompt()

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: Any,
        parent_message: Any,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        query: str = args.query
        max_results = int(args.max_results) if args.max_results is not None else 5

        tools = context.options.tools
        deferred_tools = [t for t in tools if is_deferred_tool(t)]
        _maybe_invalidate_cache(deferred_tools)

        def get_pending_server_names() -> list[str] | None:
            app_state = context.get_app_state() if context.get_app_state else None
            if not app_state:
                return None
            mcp = app_state.get("mcp") if isinstance(app_state, dict) else getattr(
                app_state, "mcp", None
            )
            clients = (mcp or {}).get("clients", []) if isinstance(mcp, dict) else []
            pending = [c for c in clients if (c or {}).get("type") == "pending"]
            return [c.get("name") for c in pending] if pending else None

        def log_search_outcome(matches: list[str], query_type: str) -> None:
            pass

        # select: prefix — direct multi-select (comma separated). A name not in the deferred set
        # but present in the full pool is still returned (already loaded -> harmless no-op).
        select_match = _SELECT_RE.match(query)
        if select_match:
            requested = [s.strip() for s in select_match.group(1).split(",") if s.strip()]
            found: list[str] = []
            missing: list[str] = []
            for tool_name in requested:
                tool = find_tool_by_name(deferred_tools, tool_name) or find_tool_by_name(
                    tools, tool_name
                )
                if tool is not None:
                    if tool.name not in found:
                        found.append(tool.name)
                else:
                    missing.append(tool_name)

            if not found:
                log_for_debugging(
                    f"ToolSearchTool: select failed — none found: {', '.join(missing)}"
                )
                log_search_outcome([], "select")
                pending_servers = get_pending_server_names()
                return _build_search_result(
                    [], query, len(deferred_tools), pending_servers
                )

            if missing:
                log_for_debugging(
                    "ToolSearchTool: partial select — found: "
                    f"{', '.join(found)}, missing: {', '.join(missing)}"
                )
            else:
                log_for_debugging(f"ToolSearchTool: selected {', '.join(found)}")
            log_search_outcome(found, "select")
            return _build_search_result(found, query, len(deferred_tools))

        # Keyword search.
        matches = await search_tools_with_keywords(
            query, deferred_tools, tools, max_results
        )
        log_for_debugging(
            f'ToolSearchTool: keyword search for "{query}", found {len(matches)} matches'
        )
        log_search_outcome(matches, "keyword")

        if not matches:
            pending_servers = get_pending_server_names()
            return _build_search_result(
                matches, query, len(deferred_tools), pending_servers
            )
        return _build_search_result(matches, query, len(deferred_tools))

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        matches = data.get("matches", []) or []
        pending = data.get("pending_mcp_servers") or []

        if len(matches) == 0:
            text = "No matching deferred tools found"
            if pending:
                text += (
                    ". Some MCP servers are still connecting: "
                    f"{', '.join(pending)}. Their tools will become available shortly — "
                    "try searching again."
                )
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
            }

        # tool_reference blocks: the wire signal that loads each matched tool's schema.
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [
                {"type": "tool_reference", "tool_name": name} for name in matches
            ],
        }


# Singleton instance (parity with `export const ToolSearchTool`).
tool_search_tool = ToolSearchTool()
