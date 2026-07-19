"""Transcript "selectable" user-message predicate

:func:`selectable_user_messages_filter` is the predicate used to pick the user messages a user can
re-select / edit in the transcript: real user prompts, excluding tool-result echoes, synthetic
control messages, meta/compact/transcript-only messages, and any message whose final text block is
terminal/bash/local-command/task-notification/tick/teammate output (matched by an opening tag).

Casing: messages are plain ``dict`` envelopes (TS loose tagged objects). Wire keys are camelCase
(``isMeta``/``isCompactSummary``/``isVisibleInTranscriptOnly``) and Anthropic snake (``tool_result``,
``text``) — preserved verbatim; only Python identifiers are snake_case.

Dependency wiring:
- ``BASH_*``/``LOCAL_COMMAND_*``/``TASK_NOTIFICATION_TAG``/``TICK_TAG``/``TEAMMATE_MESSAGE_TAG`` →
  the REAL implemented :mod:`tabvis.constants.xml`.
- ``isSyntheticMessage`` is **not** exported by :mod:`tabvis.utils.messages` (which provides only the
  INTERRUPT constants + envelope constructors), so it is reproduced here as
  :func:`_is_synthetic_message` over the ``SYNTHETIC_MESSAGES`` set defined below.
"""

from __future__ import annotations

from typing import Any

from tabvis.constants.xml import (
    BASH_STDERR_TAG,
    BASH_STDOUT_TAG,
    LOCAL_COMMAND_STDERR_TAG,
    LOCAL_COMMAND_STDOUT_TAG,
    TASK_NOTIFICATION_TAG,
    TEAMMATE_MESSAGE_TAG,
    TICK_TAG,
)

# --- synthetic-message set -----------------------------
# These are the synthetic control strings that, when they are the first text block of a message,
# mark the whole message as synthetic (interrupts / cancels / rejects / no-response sentinels).
INTERRUPT_MESSAGE = "[Request interrupted by user]"
INTERRUPT_MESSAGE_FOR_TOOL_USE = "[Request interrupted by user for tool use]"
CANCEL_MESSAGE = (
    "The user doesn't want to take this action right now. STOP what you are doing and "
    "wait for the user to tell you how to proceed."
)
REJECT_MESSAGE = (
    "The user doesn't want to proceed with this tool use. The tool use was rejected (eg. "
    "if it was a file edit, the new_string was NOT written to the file). STOP what you are "
    "doing and wait for the user to tell you how to proceed."
)
NO_RESPONSE_REQUESTED = "No response requested."

SYNTHETIC_MESSAGES: frozenset[str] = frozenset(
    {
        INTERRUPT_MESSAGE,
        INTERRUPT_MESSAGE_FOR_TOOL_USE,
        CANCEL_MESSAGE,
        REJECT_MESSAGE,
        NO_RESPONSE_REQUESTED,
    }
)


def _is_text_block(block: Any) -> bool:
    """Type guard: a ``{'type': 'text', 'text': <str>}`` content block."""
    return (
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _is_synthetic_message(message: dict[str, Any]) -> bool:
    """Return whether synthetic message.

    A message is synthetic when its type isn't progress/attachment/system, its inner content is a
    list, the first block is a text block, and that text is one of ``SYNTHETIC_MESSAGES``.
    """
    msg_type = message.get("type")
    if msg_type in ("progress", "attachment", "system"):
        return False
    content = message.get("message", {}).get("content")
    if not isinstance(content, list) or not content:
        return False
    first = content[0]
    return (
        isinstance(first, dict)
        and first.get("type") == "text"
        and first.get("text") in SYNTHETIC_MESSAGES
    )


def selectable_user_messages_filter(message: dict[str, Any]) -> bool:
    """Whether ``message`` is a re-selectable user prompt in the transcript.

    Excludes: non-user messages; tool-result echoes (first block ``tool_result``); synthetic control
    messages; meta / compact-summary / transcript-only messages; and messages whose final text block
    is terminal/bash/local-command/task-notification/tick/teammate output.
    """
    if message.get("type") != "user":
        return False

    content = message.get("message", {}).get("content")

    if (
        isinstance(content, list)
        and content
        and isinstance(content[0], dict)
        and content[0].get("type") == "tool_result"
    ):
        return False

    if _is_synthetic_message(message):
        return False
    if message.get("isMeta"):
        return False
    if message.get("isCompactSummary") or message.get("isVisibleInTranscriptOnly"):
        return False

    if isinstance(content, str):
        last_block = None
    else:
        last_block = content[-1] if isinstance(content, list) and content else None

    if isinstance(content, str):
        message_text = content.strip()
    elif last_block is not None and _is_text_block(last_block):
        message_text = last_block["text"].strip()
    else:
        message_text = ""

    terminal_tags = (
        LOCAL_COMMAND_STDOUT_TAG,
        LOCAL_COMMAND_STDERR_TAG,
        BASH_STDOUT_TAG,
        BASH_STDERR_TAG,
        TASK_NOTIFICATION_TAG,
        TICK_TAG,
        TEAMMATE_MESSAGE_TAG,
    )
    return not any(f"<{tag}" in message_text for tag in terminal_tags)
