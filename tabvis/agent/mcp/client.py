"""MCP connection orchestrator.

Handles the *connect, list tools/resources, then wrap* path for MCP servers:

* :func:`connect_mcp_server` — connects to one server over the two transports supported here:
  ``stdio`` (``stdio_client`` + ``ClientSession``) and ``http`` (``streamablehttp_client`` +
  ``ClientSession``). The session must stay OPEN for the whole run, so the two async context
  managers are entered onto a :class:`contextlib.AsyncExitStack` whose ``aclose`` becomes the
  connection's ``cleanup``. Returns a :class:`ConnectedMCPServer` on success, a
  :class:`FailedMCPServer` on any error.

* :func:`get_mcp_tools_commands_and_resources` — connects each config; for every connected server
  lists tools (wrapped via :func:`tabvis.agent.tools.mcp_tool.create_mcp_tool`) and resources (tagged
  with the server name via :class:`ServerResource`), invoking the optional
  ``on_connection_attempt`` callback per server and returning the aggregate
  ``{clients, tools, resources}``. One bad server does not abort the batch.

* :func:`cleanup_all` — awaits every connected server's ``cleanup`` (best-effort).

Casing: Python identifiers are snake_case; the ``mcp_info`` / resource wire keys come from the
:mod:`tabvis.agent.mcp.types` models — no new models are introduced here.

Not supported: auth/OAuth + ``needs-auth`` 401 caching, reconnection / session-expiry retry,
elicitation handlers, the ``sse`` / ``ws`` / ``ws-ide`` / ``sse-ide`` transports, the ``sdk``
control transport and IDE transports, MCP *commands* (prompts/list — no commands are returned),
connection timeout + SIGINT/SIGTERM/SIGKILL stdio-process escalation in ``cleanup`` (the SDK's own
``aclose`` handles teardown here), per-type concurrency batching with local/remote batch sizes
(servers are connected serially), memoization caches, and resource-tool injection
(ListMcpResources/ReadMcpResource) into the first resource-capable server's tool list.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from tabvis.agent.mcp.types import (
    ConnectedMCPServer,
    FailedMCPServer,
    McpServerConfig,
    MCPServerConnection,
    ScopedMcpServerConfig,
    ServerResource,
)
from tabvis.agent.tools.mcp_tool import create_mcp_tool
from tabvis.utils.debug import log_for_debugging

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tabvis.tool import Tool

# Cap on MCP server instructions sent to the model.
MAX_MCP_DESCRIPTION_LENGTH = 2048


# ----------------------------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------------------------


def _error_message(error: BaseException) -> str:
    """The exception's message, falling back to its class name."""
    return str(error) or error.__class__.__name__


def _is_local(config: McpServerConfig) -> bool:
    """Whether *config* is a local server: stdio / sdk / untyped configs are local."""
    server_type = getattr(config, "type", None)
    return not server_type or server_type in ("stdio", "sdk")


def _truncate_instructions(raw: str | None) -> str | None:
    """Truncate server instructions to :data:`MAX_MCP_DESCRIPTION_LENGTH`, if needed."""
    if raw and len(raw) > MAX_MCP_DESCRIPTION_LENGTH:
        return raw[:MAX_MCP_DESCRIPTION_LENGTH] + "… [truncated]"
    return raw


def _capabilities_dict(init_result: Any) -> dict[str, Any]:
    """Extract a plain capabilities dict from the SDK ``InitializeResult``.

    The SDK returns a pydantic ``ServerCapabilities``; tabvis stores capabilities as a plain dict.
    ``model_dump(exclude_none=True)`` keeps only the declared capability sections (``tools`` /
    ``resources`` / ``prompts`` ...), so a missing section is falsy for the truthiness checks
    downstream.
    """
    capabilities = getattr(init_result, "capabilities", None)
    if capabilities is None:
        return {}
    if hasattr(capabilities, "model_dump"):
        return capabilities.model_dump(exclude_none=True)
    if isinstance(capabilities, dict):
        return {k: v for k, v in capabilities.items() if v is not None}
    return {}


# ----------------------------------------------------------------------------------------------
# Connect one server
# ----------------------------------------------------------------------------------------------


async def connect_mcp_server(
    name: str,
    scoped_config: ScopedMcpServerConfig,
) -> MCPServerConnection:
    """Connect to ONE MCP server and return its connection state.

    Handles ``stdio`` (default / untyped) and ``http`` transports. On any failure returns
    :class:`FailedMCPServer`.

    The session is kept OPEN for the lifetime of the run: ``stdio_client`` / ``streamablehttp_client``
    and the :class:`ClientSession` are entered onto a :class:`contextlib.AsyncExitStack`, and that
    stack's ``aclose`` is stored as the connection ``cleanup``. ``session.initialize()`` is awaited
    before the session is handed back so the capabilities / serverInfo / instructions are populated.

    Not supported: connection timeout race, the SIGINT/SIGTERM/SIGKILL stdio-process escalation,
    auth / ``needs-auth`` handling, reconnection, and the sse/ws/sdk/ide transports.
    """
    config = scoped_config.config
    server_type = getattr(config, "type", None) or "stdio"

    if server_type in ("sdk", "sse-ide", "ws-ide"):
        # Unsupported transport type; surface as failed so the batch keeps going.
        log_for_debugging(f"[MCP] Skipping unsupported transport '{server_type}' for '{name}'")
        return FailedMCPServer(
            name=name,
            config=scoped_config,
            error=f"Unsupported server type for headless skeleton: {server_type}",
        )

    # Lazy imports so the module stays importable even if a transport extra is missing, and so the
    # in-memory test path (which patches connect_mcp_server) never pays for them.
    from mcp import ClientSession

    exit_stack = contextlib.AsyncExitStack()
    try:
        if server_type in ("stdio",) or not getattr(config, "type", None):
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=config.env,
            )
            read, write = await exit_stack.enter_async_context(stdio_client(params))
        elif server_type == "http":
            from mcp.client.streamable_http import streamable_http_client

            # streamable_http_client yields (read, write, get_session_id).
            read, write, _get_session_id = await exit_stack.enter_async_context(
                streamable_http_client(config.url, headers=getattr(config, "headers", None))
            )
        else:
            raise ValueError(f"Unsupported server type: {server_type}")

        session = await exit_stack.enter_async_context(ClientSession(read, write))
        init_result = await session.initialize()

        capabilities = _capabilities_dict(init_result)
        server_info = getattr(init_result, "serverInfo", None)
        if server_info is not None and hasattr(server_info, "model_dump"):
            server_info = server_info.model_dump(exclude_none=True)
        instructions = _truncate_instructions(getattr(init_result, "instructions", None))

        cleanup = exit_stack.aclose
        return ConnectedMCPServer(
            client=session,
            name=name,
            capabilities=capabilities,
            config=scoped_config,
            cleanup=cleanup,
            server_info=server_info if isinstance(server_info, dict) else None,
            instructions=instructions,
        )
    except BaseException as error:
        # A failed transport connect surfaces here as a plain Exception OR, because the mcp SDK
        # transports run inside anyio task groups with internal cancel scopes, as an anyio-wrapped
        # CancelledError / ExceptionGroup. Both are classified as a connection failure; teardown of
        # the partially entered stack is best-effort and its own anyio cancel-scope noise is
        # suppressed.
        with contextlib.suppress(BaseException):
            await exit_stack.aclose()
        if isinstance(error, KeyboardInterrupt):
            raise
        log_for_debugging(f"[MCP] Connection failed for '{name}': {_error_message(error)}")
        return FailedMCPServer(name=name, config=scoped_config, error=_error_message(error))


# ----------------------------------------------------------------------------------------------
# Fetch tools / resources for a connected server
# ----------------------------------------------------------------------------------------------


async def _fetch_tools_for_client(client: MCPServerConnection) -> list[Tool]:
    """List and wrap a connected server's tools.

    Returns ``[]`` unless the server is connected AND declares the ``tools`` capability. Each MCP
    tool definition is wrapped into a tabvis :class:`Tool` via :func:`create_mcp_tool` (the
    ``mcp__server__tool`` normalization lives there).
    """
    if not isinstance(client, ConnectedMCPServer):
        return []
    if not client.capabilities.get("tools"):
        return []
    try:
        result = await client.client.list_tools()
    except (Exception, BaseExceptionGroup) as error:  # noqa: BLE001 - anyio wraps in groups
        log_for_debugging(f"[MCP] Failed to fetch tools for '{client.name}': {_error_message(error)}")
        return []
    tools: list[Tool] = []
    for tool_def in getattr(result, "tools", None) or []:
        tools.append(create_mcp_tool(client, tool_def))
    return tools


async def _fetch_resources_for_client(client: MCPServerConnection) -> list[ServerResource]:
    """List a connected server's resources.

    Returns ``[]`` unless connected AND the server declares the ``resources`` capability. Each MCP
    resource is tagged with ``server=client.name`` and validated into a :class:`ServerResource`
    (``extra='allow'`` keeps any extra MCP fields). Resources whose shape is unexpected are skipped
    rather than aborting the fetch.
    """
    if not isinstance(client, ConnectedMCPServer):
        return []
    if not client.capabilities.get("resources"):
        return []
    try:
        result = await client.client.list_resources()
    except (Exception, BaseExceptionGroup) as error:  # noqa: BLE001 - anyio wraps in groups
        log_for_debugging(
            f"[MCP] Failed to fetch resources for '{client.name}': {_error_message(error)}"
        )
        return []
    resources: list[ServerResource] = []
    for resource in getattr(result, "resources", None) or []:
        raw = resource.model_dump() if hasattr(resource, "model_dump") else dict(resource)
        raw["server"] = client.name
        raw["uri"] = str(raw.get("uri", ""))
        try:
            resources.append(ServerResource.model_validate(raw))
        except Exception as error:  # noqa: BLE001
            log_for_debugging(
                f"[MCP] Skipping malformed resource from '{client.name}': {_error_message(error)}"
            )
    return resources


# Per-server connection-attempt payload passed to on_connection_attempt.
ConnectionAttempt = dict[str, Any]


# ----------------------------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------------------------


async def get_mcp_tools_commands_and_resources(
    configs: dict[str, ScopedMcpServerConfig],
    on_connection_attempt: Callable[[ConnectionAttempt], None] | None = None,
) -> dict[str, Any]:
    """Connect every configured server and aggregate its tools + resources.

    For each config:

    * connect via :func:`connect_mcp_server`;
    * for a :class:`ConnectedMCPServer`, list + wrap its tools and list its resources;
    * invoke ``on_connection_attempt({"client", "tools", "commands", "resources"})`` per server —
      ``commands`` is always ``[]`` here (MCP prompts are not supported);
    * collect the connection into ``clients``, the wrapped tools into ``tools``, and the resources
      into ``resources[server_name]``.

    Returns ``{"clients": [...], "tools": [...], "resources": {server: [ServerResource]}}``. A
    failure connecting or fetching one server is isolated to that server (it lands as a
    :class:`FailedMCPServer` / yields no tools) and never aborts the batch.

    Not supported: per-type concurrency batching (local vs remote batch sizes), the ``needs-auth``
    401 cache short-circuit, MCP *commands* (prompts), and resource-tool injection.
    """
    clients: list[MCPServerConnection] = []
    all_tools: list[Tool] = []
    resources_by_server: dict[str, list[ServerResource]] = {}

    for name, scoped_config in configs.items():
        try:
            client = await connect_mcp_server(name, scoped_config)
            clients.append(client)

            if not isinstance(client, ConnectedMCPServer):
                if on_connection_attempt is not None:
                    on_connection_attempt(
                        {"client": client, "tools": [], "commands": [], "resources": None}
                    )
                continue

            tools = await _fetch_tools_for_client(client)
            resources = await _fetch_resources_for_client(client)

            all_tools.extend(tools)
            if resources:
                resources_by_server[name] = resources

            if on_connection_attempt is not None:
                on_connection_attempt(
                    {
                        "client": client,
                        "tools": tools,
                        "commands": [],
                        "resources": resources or None,
                    }
                )
        except (Exception, BaseExceptionGroup) as error:  # noqa: BLE001 - isolate per-server failures
            log_for_debugging(
                f"[MCP] Error processing server '{name}': {_error_message(error)}"
            )
            failed = FailedMCPServer(name=name, config=scoped_config, error=_error_message(error))
            clients.append(failed)
            if on_connection_attempt is not None:
                on_connection_attempt(
                    {"client": failed, "tools": [], "commands": [], "resources": None}
                )

    return {"clients": clients, "tools": all_tools, "resources": resources_by_server}


# ----------------------------------------------------------------------------------------------
# Cleanup
# ----------------------------------------------------------------------------------------------


async def cleanup_all(clients: list[MCPServerConnection]) -> None:
    """Await every connected server's ``cleanup`` (best-effort).

    Only :class:`ConnectedMCPServer` entries carry a ``cleanup`` (the ``AsyncExitStack.aclose``
    from :func:`connect_mcp_server`); failed/disabled entries are skipped. Each cleanup is isolated
    so one failing teardown does not block the rest.
    """
    for client in clients:
        if not isinstance(client, ConnectedMCPServer):
            continue
        cleanup: Callable[[], Awaitable[None]] | None = getattr(client, "cleanup", None)
        if cleanup is None:
            continue
        try:
            await cleanup()
        except Exception as error:  # noqa: BLE001
            log_for_debugging(
                f"[MCP] Error during cleanup of '{client.name}': {_error_message(error)}"
            )
