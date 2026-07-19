"""Query helpers

``normalize_message`` is the ONE casing boundary: internal camelCase message envelopes →
snake_case SDKMessages for ``-p`` output (assistant/user; system + progress drop for the
skeleton). ``build_system_init_message`` is the ``{type:'system', subtype:'init'}`` SDKMessage.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

from tabvis.bootstrap_macro import MACRO
from tabvis.constants.messages import NO_CONTENT_MESSAGE
from tabvis.agent.api.client import get_session_id
from tabvis.tool import Tools

SYNTHETIC_MESSAGES = {NO_CONTENT_MESSAGE}


def _is_not_empty_message(message: dict[str, Any]) -> bool:
    content = (message.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if block.get("type") != "text":
                return True
            text = (block.get("text") or "").strip()
            if text and block.get("text") != NO_CONTENT_MESSAGE:
                return True
        return False
    return bool(content)


def normalize_message(message: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Map an internal message to zero-or-more SDKMessages (the snake_case boundary)."""
    t = message.get("type")
    if t == "assistant":
        if _is_not_empty_message(message):
            yield {
                "type": "assistant",
                "message": message["message"],
                "parent_tool_use_id": None,
                "session_id": get_session_id(),
                "uuid": message.get("uuid"),
                "error": message.get("error"),
            }
        return
    if t == "user":
        tool_use_result = message.get("toolUseResult")
        mcp_meta = message.get("mcpMeta")
        yield {
            "type": "user",
            "message": message["message"],
            "parent_tool_use_id": None,
            "session_id": get_session_id(),
            "uuid": message.get("uuid"),
            "timestamp": message.get("timestamp"),
            "isSynthetic": bool(
                message.get("isMeta") or message.get("isVisibleInTranscriptOnly")
            ),
            "tool_use_result": (
                {"content": tool_use_result, **mcp_meta} if mcp_meta else tool_use_result
            ),
        }
        return
    # system sentinel / progress: nothing for the skeleton (dropped at the SDK boundary).


def build_system_init_message(
    session_id: str, cwd: str, tools: Tools, model: str
) -> dict[str, Any]:
    """Build the SDK initialization event for a headless agent run."""
    return {
        "type": "system",
        "subtype": "init",
        "cwd": cwd,
        "session_id": session_id,
        "tools": [t.name for t in tools],
        "mcp_servers": [],
        "model": model,
        "permissionMode": "default",
        "slash_commands": [],
        "apiKeySource": "env",
        "betas": [],
        "tabvis_version": MACRO.VERSION,
        "output_style": "default",
        "agents": [],
        "skills": [],
        "uuid": str(uuid.uuid4()),
    }
