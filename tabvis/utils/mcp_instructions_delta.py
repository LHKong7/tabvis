"""MCP instructions delta

Diffs the set of connected MCP servers that carry instructions (server-authored via
``InitializeResult`` or client-side synthesized) against what has already been announced in the
conversation (via persisted ``mcp_instructions_delta`` attachment messages). Returns a delta of
newly-added blocks + removed names, or ``None`` if nothing changed.

Casing: Python identifiers are snake_case. The :class:`McpInstructionsDelta` /
:class:`ClientSideInstruction` payloads are dict-shaped data that round-trips into the transcript
as attachment fields, so their keys stay verbatim wire keys (``addedNames``/``addedBlocks``/
``removedNames``; ``serverName``/``block``). The analytics ``logEvent`` metadata likewise keeps its
camelCase wire keys. Internal idents (function/local names) are snake_case.

Messages are walked as plain ``dict`` (the Tabvis transcript form): attachment messages read
``msg["attachment"]`` with its ``type`` / ``addedNames`` / ``removedNames`` keys.
"""

from __future__ import annotations

import os
from typing import Any, TypedDict

from tabvis.agent.mcp.types import ConnectedMCPServer, MCPServerConnection
from tabvis.types.message import Message
from tabvis.utils.env_utils import is_env_defined_falsy, is_env_truthy

__all__ = [
    "ClientSideInstruction",
    "McpInstructionsDelta",
    "get_mcp_instructions_delta",
    "is_mcp_instructions_delta_enabled",
]


class McpInstructionsDelta(TypedDict):
    """The delta payload (wire keys preserved for transcript round-trip)."""

    # Server names — for stateless-scan reconstruction.
    addedNames: list[str]
    # Rendered "## {name}\n{instructions}" blocks for addedNames.
    addedBlocks: list[str]
    removedNames: list[str]


class ClientSideInstruction(TypedDict):
    """Client-authored instruction block announced when a server connects.

    Lets first-party servers carry client-side context the server itself doesn't know about.
    """

    serverName: str
    block: str


def is_mcp_instructions_delta_enabled() -> bool:
    """Whether to announce MCP server instructions via persisted delta attachments.

    ``TABVIS_MCP_INSTR_DELTA=true/false`` (env override for local testing) wins over both the ant
    bypass and the GrowthBook gate.
    """
    if is_env_truthy(os.environ.get("TABVIS_MCP_INSTR_DELTA")):
        return True
    if is_env_defined_falsy(os.environ.get("TABVIS_MCP_INSTR_DELTA")):
        return False
    return False


def get_mcp_instructions_delta(
    mcp_clients: list[MCPServerConnection],
    messages: list[Message],
    client_side_instructions: list[ClientSideInstruction],
) -> McpInstructionsDelta | None:
    """Diff connected MCP servers with instructions against what's already been announced.

    Instructions are immutable for the life of a connection (set once at handshake), so the scan
    diffs on server NAME, not on content. Returns ``None`` if nothing changed.
    """
    announced: set[str] = set()
    attachment_count = 0
    mid_count = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") != "attachment":
            continue
        attachment_count += 1
        attachment: Any = msg.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "mcp_instructions_delta":
            continue
        mid_count += 1
        for n in attachment.get("addedNames", []):
            announced.add(n)
        for n in attachment.get("removedNames", []):
            announced.discard(n)

    connected = [c for c in mcp_clients if isinstance(c, ConnectedMCPServer)]
    connected_names = {c.name for c in connected}

    # Servers with instructions to announce (either channel). A server can have both:
    # server-authored instructions + a client-side block appended.
    blocks: dict[str, str] = {}
    for c in connected:
        if c.instructions:
            blocks[c.name] = f"## {c.name}\n{c.instructions}"
    for ci in client_side_instructions:
        if ci["serverName"] not in connected_names:
            continue
        existing = blocks.get(ci["serverName"])
        blocks[ci["serverName"]] = (
            f"{existing}\n\n{ci['block']}"
            if existing
            else f"## {ci['serverName']}\n{ci['block']}"
        )

    added: list[dict[str, str]] = []
    for name, block in blocks.items():
        if name not in announced:
            added.append({"name": name, "block": block})

    # A previously-announced server that is no longer connected -> removed. There is no
    # "announced but now has no instructions" case for a still-connected server: InitializeResult
    # is immutable, and client-side instruction gates are session-stable in practice.
    removed: list[str] = [n for n in announced if n not in connected_names]

    if not added and not removed:
        return None

    added.sort(key=lambda a: a["name"])
    return {
        "addedNames": [a["name"] for a in added],
        "addedBlocks": [a["block"] for a in added],
        "removedNames": sorted(removed),
    }
