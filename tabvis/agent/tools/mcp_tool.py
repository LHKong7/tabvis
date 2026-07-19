"""MCP tool wrappers for tools discovered from connected servers.

:func:`create_mcp_tool` binds one ``ConnectedMCPServer`` and one MCP tool definition to a Tabvis
:class:`Tool`. Names are normalized as ``mcp__<normalized server>__<normalized tool>``.

Casing: Python identifiers snake_case; ``mcp_info`` keeps the wire keys ``serverName``/``toolName``
(round-trips to the permission system + transcript), and the ``tool_result`` block keeps the
Anthropic snake wire keys ``tool_use_id`` / ``is_error``.

Not supported: URL-elicitation retry, session-expiry reconnection, progress emission,
result truncation / image persistence-to-disk, blob/audio/resource transforms beyond text+image,
and the SDK ``TABVIS_AGENT_SDK_MCP_NO_PREFIX`` skip-prefix mode — none are needed for this build's
stdio/http + in-memory call path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from tabvis.agent.mcp.mcp_string_utils import build_mcp_tool_name
from tabvis.tool import Tool, ToolResult, ToolUseContext

if TYPE_CHECKING:
    from tabvis.agent.mcp.types import ConnectedMCPServer
    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.message import AssistantMessage

# Cap on MCP tool descriptions sent to the model (client.ts:204 MAX_MCP_DESCRIPTION_LENGTH).
MAX_MCP_DESCRIPTION_LENGTH = 2048

# Tool result persistence threshold (MCPTool.ts: maxResultSizeChars: 100_000).
MCP_MAX_RESULT_SIZE_CHARS = 100_000

# ----------------------------------------------------------------------------------------------
# Permissive input schema (lazySchema(() => z.object({}).passthrough()) -> pydantic extra='allow')
# ----------------------------------------------------------------------------------------------


class MCPToolInput(BaseModel):
    """Permissive input model for MCP tools.

    MCP tools define their
    own JSON Schemas, so the tabvis-side validator must accept any arg shape. ``extra='allow'`` keeps
    all passed args; the *advertised* schema is the MCP tool's own ``inputJSONSchema`` (set on the
    tool instance), which is what the model actually sees.
    """

    model_config = ConfigDict(extra="allow")


def _content_block_to_data(block: Any) -> dict[str, Any] | None:
    """Map a single MCP ``CallToolResult`` content block into an Anthropic content-block dict.

    Faithful (bounded)
    needs: ``text`` -> ``{type:'text', text}`` and ``image`` -> ``{type:'image', source:{...}}``.

    Audio / resource / resource_link blocks (persist-to-disk, image resize) are not supported.
    Unknown kinds are dropped.
    """
    block_type = _attr(block, "type")
    if block_type == "text":
        return {"type": "text", "text": _attr(block, "text") or ""}
    if block_type == "image":
        data = _attr(block, "data")
        mime_type = _attr(block, "mimeType") or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime_type, "data": data},
        }
    return None


def _attr(obj: Any, key: str) -> Any:
    """Read ``key`` from a pydantic model / object / dict (MCP SDK returns pydantic models)."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def map_mcp_result_content(result: Any) -> list[dict[str, Any]]:
    """Map a ``CallToolResult`` (``.content`` list of blocks) into Anthropic content-block dicts."""
    content = _attr(result, "content") or []
    blocks: list[dict[str, Any]] = []
    for item in content:
        mapped = _content_block_to_data(item)
        if mapped is not None:
            blocks.append(mapped)
    return blocks


def _unwrap_mcp_data(data: Any) -> tuple[Any, bool]:
    """Split the :meth:`MCPToolWrapper.call` envelope into ``(inner_content, is_error)``.

    Accepts the ``{"content", "is_error"}`` envelope or a raw content value (for robustness).
    """
    if isinstance(data, dict) and "content" in data and "is_error" in data:
        return data["content"], bool(data["is_error"])
    return data, False


# ----------------------------------------------------------------------------------------------
# The wrapped tool
# ----------------------------------------------------------------------------------------------


class MCPToolWrapper(Tool):
    """One MCP server tool surfaced as a tabvis :class:`Tool`.

    Built by :func:`create_mcp_tool`; not instantiated directly elsewhere. Mirrors the per-tool
    object the TS ``mcpClient`` spreads over ``MCPTool`` (``client.ts`` ~1534-1741).
    """

    input_schema = MCPToolInput
    max_result_size_chars = MCP_MAX_RESULT_SIZE_CHARS
    is_mcp = True

    def __init__(self, connected_server: ConnectedMCPServer, mcp_tool_def: Any) -> None:
        self._server = connected_server
        self._def = mcp_tool_def
        server_name = connected_server.name
        tool_name = _attr(mcp_tool_def, "name")
        self._server_name = server_name
        self._tool_name = tool_name
        # Overridden in mcpClient.ts: fully qualified mcp__server__tool name.
        self.name = build_mcp_tool_name(server_name, tool_name)
        # mcpInfo is used for permission checking; keep the wire keys.
        self.mcp_info = {"serverName": server_name, "toolName": tool_name}
        # The MCP tool's own JSON Schema is what the model sees (client.ts:1577).
        self.input_json_schema = _attr(mcp_tool_def, "inputSchema") or {"type": "object"}
        self._description = _attr(mcp_tool_def, "description") or ""
        self._annotations = _attr(mcp_tool_def, "annotations")

    # --- description / prompt (overridden in mcpClient.ts) ---
    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return self._description

    async def prompt(self, options: dict[str, Any]) -> str:
        desc = self._description
        if len(desc) > MAX_MCP_DESCRIPTION_LENGTH:
            return desc[:MAX_MCP_DESCRIPTION_LENGTH] + "… [truncated]"
        return desc

    # --- annotation-driven flags (client.ts:1562-1573) ---
    def _annotation(self, key: str) -> Any:
        return _attr(self._annotations, key) if self._annotations is not None else None

    def is_concurrency_safe(self, input: Any) -> bool:
        return bool(self._annotation("readOnlyHint") or False)

    def is_read_only(self, input: Any) -> bool:
        return bool(self._annotation("readOnlyHint") or False)

    def is_destructive(self, input: Any) -> bool:
        return bool(self._annotation("destructiveHint") or False)

    def is_open_world(self, input: Any) -> bool:
        return bool(self._annotation("openWorldHint") or False)

    def user_facing_name(self, input: Any | None = None) -> str:
        # Prefer the title annotation if available, otherwise the tool name (client.ts:1736-1739).
        display_name = self._annotation("title") or self._tool_name
        return f"{self._server_name} - {display_name} (MCP)"

    async def check_permissions(self, input: Any, context: ToolUseContext) -> dict[str, Any]:
        # MCP tools defer to the general permission system via 'passthrough' (client.ts:1578).
        return {"behavior": "passthrough", "message": "MCPTool requires permission."}

    def is_result_truncated(self, output: Any) -> bool:
        return False

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        """Build the Anthropic ``tool_result`` block (MCPTool.ts:62-68 + is_error).

        ``content`` is the :class:`ToolResult` ``data``, the envelope :meth:`call` produces:
        ``{"content": [block...], "is_error": bool}``. The block's ``content`` is the inner content
        list (the TS ``MCPTool`` returns ``content`` directly); ``is_error`` is threaded from the
        source MCP ``CallToolResult.isError`` and only emitted when true (matches the TS spread).
        """
        inner, is_error = _unwrap_mcp_data(content)
        block: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": inner,
        }
        if is_error:
            block["is_error"] = True
        return block

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[Any]:
        """Invoke the MCP tool via ``connected_server.client.call_tool`` and map the result.

        URL-elicitation retry, session-expiry reconnection (single retry), and progress emission
        are not supported; this build makes one direct call.
        """
        args_dict = _to_args_dict(args)
        session = self._server.client  # mcp.ClientSession
        result = await session.call_tool(self._tool_name, args_dict)

        data = map_mcp_result_content(result)
        is_error = bool(_attr(result, "isError"))

        # Stable envelope so data round-trips identically whether or not the call errored, and so
        # map_tool_result_to_tool_result_block_param can surface is_error from the same shape.
        tool_result: ToolResult[Any] = ToolResult(
            data={"content": data, "is_error": is_error}
        )

        # MCP protocol metadata (_meta / structuredContent) passed through to SDK consumers
        # (client.ts:1663-1672).
        meta = _attr(result, "meta")
        structured = _attr(result, "structuredContent")
        if meta or structured:
            mcp_meta: dict[str, Any] = {}
            if meta:
                mcp_meta["_meta"] = meta
            if structured:
                mcp_meta["structuredContent"] = structured
            tool_result.mcp_meta = mcp_meta

        return tool_result


def _to_args_dict(args: Any) -> dict[str, Any]:
    """Normalize tool args (pydantic model / object / dict) into a plain dict for the MCP call."""
    if args is None:
        return {}
    if isinstance(args, dict):
        return dict(args)
    if isinstance(args, BaseModel):
        return args.model_dump()
    if hasattr(args, "__dict__"):
        return {k: v for k, v in vars(args).items() if not k.startswith("_")}
    return {}


def create_mcp_tool(connected_server: ConnectedMCPServer, mcp_tool_def: Any) -> Tool:
    """Turn ONE MCP server tool into a tabvis :class:`Tool` instance.

    The resulting tool's ``name`` is ``mcp__<server>__<tool>``, ``is_mcp=True``,
    ``mcp_info={serverName, toolName}``,
    ``input_json_schema`` = the MCP tool's own JSON Schema, and whose ``call`` invokes
    ``connected_server.client.call_tool``.
    """
    return MCPToolWrapper(connected_server, mcp_tool_def)
