"""Transcript mapping for the Context Runtime (design §11.3 #4).

tabvis stores a transcript as a `parentUuid`-linked set of entries; the Context Runtime's
`TranscriptProvider` wants a simple ordered list of ``{id, role, text, ts}`` messages. This module
bridges the two:

* :func:`map_transcript_messages` is a **pure** function over an already-ordered conversation chain —
  it keeps only real transcript messages (user/assistant/system/attachment), and flattens each SDK
  content list into plain text, summarizing tool-use/result/image blocks rather than dumping them.
* :func:`load_session_transcript` is the real default loader the `SourceCollector` uses: it reads the
  session's transcript file, rebuilds the leaf-to-root conversation chain, and maps it — returning ``[]``
  for a missing/empty transcript so a fresh session degrades cleanly.
"""

from __future__ import annotations

from typing import Any


def _extract_text(content: Any) -> str:
    """Flatten an SDK message ``content`` (string or block list) into plain text (design §7.9: tool
    payloads are summarized, never dumped)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            parts.append(f"[tool_use: {block.get('name', 'tool')}]")
        elif btype == "tool_result":
            parts.append("[tool_result]")
        elif btype == "image":
            parts.append("[image]")
    return " ".join(p for p in parts if p).strip()


def map_transcript_messages(chain: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map an ordered (root→leaf) conversation chain to ``[{id, role, text, ts}]``."""
    from tabvis.utils.session_storage import is_transcript_message

    out: list[dict[str, Any]] = []
    for entry in chain:
        if not is_transcript_message(entry):   # drop progress / non-message entries
            continue
        etype = entry.get("type")
        message = entry.get("message") or {}
        role = message.get("role") or etype or "user"
        text = _extract_text(message.get("content"))
        if not text and etype == "attachment":
            text = "[attachment]"
        out.append({
            "id": entry.get("uuid", ""),
            "role": role,
            "text": text,
            "ts": entry.get("timestamp"),
        })
    return out


def _pick_leaf(messages: dict[str, dict[str, Any]], leaf_uuids: Any) -> dict[str, Any] | None:
    """The conversation leaf to chain back from: the latest-timestamped declared leaf, else the latest
    message overall."""
    leaf_uuids = set(leaf_uuids or ())
    candidates = [m for m in messages.values() if m.get("uuid") in leaf_uuids]
    if not candidates:
        candidates = list(messages.values())
    if not candidates:
        return None
    return max(candidates, key=lambda m: m.get("timestamp") or "")


async def load_session_transcript(session_id: str) -> list[dict[str, Any]]:
    """Load and map a session's transcript. ``[]`` for a missing/empty one (a fresh session)."""
    import os

    from tabvis.utils.session_storage import (
        build_conversation_chain,
        get_transcript_path_for_session,
        load_transcript_file,
    )

    path = get_transcript_path_for_session(session_id)
    if not path or not os.path.exists(path):
        return []
    loaded = await load_transcript_file(path)
    messages = loaded.get("messages") or {}
    if not messages:
        return []
    leaf = _pick_leaf(messages, loaded.get("leafUuids"))
    if leaf is None:
        return []
    chain = build_conversation_chain(messages, leaf)
    return map_transcript_messages(chain)
