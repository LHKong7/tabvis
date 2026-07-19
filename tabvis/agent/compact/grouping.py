"""Message grouping by API round.

Groups messages at API-round boundaries: one group per API round-trip. A boundary fires when a
NEW assistant response begins (a different ``message.id`` from the prior assistant). For
well-formed conversations this is an API-safe split point — the API contract requires every
``tool_use`` to be resolved before the next assistant turn, so pairing validity falls out of the
assistant-id boundary. For malformed inputs (dangling ``tool_use`` after resume/truncation) the
summarizer fork's tool-result pairing repair fixes up the split at API time.

Kept in its own module to avoid a circular dependency with the message-summarization module.
"""

from __future__ import annotations

from tabvis.types.message import Message


def group_messages_by_api_round(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    current: list[Message] = []
    # message.id of the most recently seen assistant. This is the sole boundary gate: streaming
    # chunks from the same API response share an id, so boundaries only fire at the start of a
    # genuinely new round. The id check correctly keeps
    # `[tu_A(id=X), result_A, tu_B(id=X)]` in one group.
    last_assistant_id: str | None = None

    # In a well-formed conversation the API contract guarantees every tool_use is resolved before
    # the next assistant turn, so last_assistant_id alone is a sufficient boundary gate. Malformed
    # boundaries are allowed to fire; the summarizer fork's tool-result pairing repair fixes up any
    # dangling tool_use at API time.
    for msg in messages:
        if (
            msg.get("type") == "assistant"
            and msg.get("message", {}).get("id") != last_assistant_id
            and len(current) > 0
        ):
            groups.append(current)
            current = [msg]
        else:
            current.append(msg)
        if msg.get("type") == "assistant":
            last_assistant_id = msg.get("message", {}).get("id")

    if len(current) > 0:
        groups.append(current)
    return groups
