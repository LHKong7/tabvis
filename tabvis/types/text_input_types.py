"""Text-input + command-queue types

This module is the home of the prompt/text-input prop shapes (consumed by the Ink/React
text-input components) AND, more importantly for the headless build, the **command-queue**
contracts that ``utils/messageQueueManager.ts`` builds on:

* :data:`QueuePriority` / :data:`PromptInputMode` / :data:`EditablePromptInputMode` — the queue
  ordering + input-mode literals.
* :class:`QueuedCommand` — the queue entry. It round-trips through the async queue boundary and
  carries wire-shaped sub-payloads (``pastedContents`` / ``orphanedPermission`` / ``origin``), so
  it is modeled as a ``TypedDict`` with **verbatim camelCase wire keys**
  (``orphanedPermission`` / ``pastedContents`` / ``preExpansionValue`` / ``skipSlashCommands`` /
  ``isMeta`` / ``agentId``), per ``docs/SPINE_CONTRACTS.md``.
* :func:`is_valid_image_paste` / :func:`get_image_paste_ids` — the two runtime helpers.
* :class:`OrphanedPermission` — the permission-result + assistant-message pairing.

The remaining exports (``Key``, ``InlineGhostText``, ``BaseTextInputProps``,
``VimTextInputProps``, ``BaseInputState``, ``TextInputState``, ``VimInputState``) are
React/Ink component prop+state shapes. They are UI-only (the headless spine never renders a
text input), but are implemented with equivalent behavior as ``TypedDict`` shapes so the type surface stays 1:1.

Casing convention (``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; dict-shaped
data that round-trips to the transcript / SDK keeps its camelCase wire keys verbatim. These
prop/queue shapes are loose React/transcript records, so they are ``TypedDict`` (NO
``extra=forbid``) — extra keys are accepted, matching the TS ``readonly`` object types.

Dependency notes:
- ``PastedContent`` lives in :mod:`tabvis.utils.image_store` (the not-yet-implemented
  ``src/utils/config.ts`` is only the ``enable_configs`` stub) — re-exported from there.
- ``PermissionResult`` → :data:`tabvis.types.permissions.PermissionResult`.
- ``AssistantMessage`` / ``MessageOrigin`` → :mod:`tabvis.types.message`.
- ``AgentId`` → :data:`tabvis.types.ids.AgentId`.
- ``ContentBlockParam`` is the Anthropic content-block param: a plain ``dict`` (as in
  ``tabvis.types.command``).
- ``ImageDimensions`` (``src/utils/imageResizer.ts``) and ``TextHighlight`` are UI-only; the
  former is not yet implemented (modeled as an opaque ``dict``), the latter is reused from
  :mod:`tabvis.utils.text_highlighting`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from tabvis.types.ids import AgentId
from tabvis.types.message import AssistantMessage
from tabvis.types.permissions import PermissionResult
from tabvis.utils.image_store import PastedContent

if TYPE_CHECKING:  # pragma: no cover - typing-only imports
    from tabvis.utils.text_highlighting import TextHighlight

__all__ = [
    "Key",
    "InlineGhostText",
    "BaseTextInputProps",
    "VimTextInputProps",
    "VimMode",
    "BaseInputState",
    "TextInputState",
    "VimInputState",
    "PromptInputMode",
    "EditablePromptInputMode",
    "QueuePriority",
    "QueuedCommand",
    "is_valid_image_paste",
    "get_image_paste_ids",
    "OrphanedPermission",
]

# A single Anthropic content-block param (``@anthropic-ai/sdk`` ``ContentBlockParam``). Per
# ``docs/SPINE_CONTRACTS.md`` API payloads keep snake_case wire keys → a plain open ``dict``.
ContentBlockParam = dict[str, Any]

# UI-only opaque placeholders (faithful to the TS ``unknown`` / not-yet-implemented imports).
PlaceholderNode = Any
ImageDimensions = dict[str, Any]


class Key(TypedDict, total=False):
    """A parsed keypress descriptor (Ink ``useInput`` key object)."""

    name: str
    ctrl: bool
    meta: bool
    shift: bool
    paste: bool


class InlineGhostText(TypedDict):
    """Inline ghost text for mid-input command autocomplete."""

    # The ghost text to display (e.g., "mit" for /commit).
    text: str
    # The full command name (e.g., "commit").
    fullCommand: str
    # Position in the input where the ghost text should appear.
    insertPosition: int


class BaseTextInputProps(TypedDict, total=False):
    """Base props for text input components (Ink/React)."""

    # Optional callback for handling history navigation on up arrow at start of input.
    onHistoryUp: Callable[[], None]
    # Optional callback for handling history navigation on down arrow at end of input.
    onHistoryDown: Callable[[], None]
    # Text to display when ``value`` is empty.
    placeholder: str
    # Allow multi-line input via line ending with backslash (default: ``True``).
    multiline: bool
    # Listen to user's input (route input to a specific component).
    focus: bool
    # Replace all chars and mask the value (password inputs).
    mask: str
    # Whether to show cursor and allow navigation inside text input with arrow keys.
    showCursor: bool
    # Highlight pasted text.
    highlightPastedText: bool
    # Value to display in a text input (required in TS).
    value: str
    # Function to call when value updates (required in TS).
    onChange: Callable[[str], None]
    # Function to call when ``Enter`` is pressed; arg is the input value.
    onSubmit: Callable[[str], None]
    # Function to call when Ctrl+C is pressed to exit.
    onExit: Callable[[], None]
    # Optional callback to show exit message.
    onExitMessage: Callable[..., None]
    # Optional callback to reset history position.
    onHistoryReset: Callable[[], None]
    # Optional callback when input is cleared (e.g., double-escape).
    onClearInput: Callable[[], None]
    # Number of columns to wrap text at (required in TS).
    columns: int
    # Maximum visible lines for the input viewport.
    maxVisibleLines: int
    # Optional callback when an image is pasted.
    onImagePaste: Callable[..., None]
    # Optional callback when a large text (over 800 chars) is pasted.
    onPaste: Callable[[str], None]
    # Callback when the pasting state changes.
    onIsPastingChange: Callable[[bool], None]
    # Whether to disable cursor movement for up/down arrow keys.
    disableCursorMovementForUpDownKeys: bool
    # Skip the text-level double-press escape handler.
    disableEscapeDoublePress: bool
    # The offset of the cursor within the text (required in TS).
    cursorOffset: int
    # Callback to set the offset of the cursor (required in TS).
    onChangeCursorOffset: Callable[[int], None]
    # Optional hint text to display after command input.
    argumentHint: str
    # Optional callback for undo functionality.
    onUndo: Callable[[], None]
    # Whether to render the text with dim color.
    dimColor: bool
    # Optional text highlights for search results or other highlighting.
    highlights: list[TextHighlight]
    # Optional custom React element to render as placeholder.
    placeholderElement: PlaceholderNode
    # Optional inline ghost text for mid-input command autocomplete.
    inlineGhostText: InlineGhostText
    # Optional filter applied to raw input before key routing.
    inputFilter: Callable[[str, Key], str]


# Vim editor modes.
VimMode = Literal["INSERT", "NORMAL"]


class VimTextInputProps(BaseTextInputProps, total=False):
    """Extended props for VimTextInput."""

    # Initial vim mode to use.
    initialMode: VimMode
    # Optional callback for mode changes.
    onModeChange: Callable[[VimMode], None]


class _PasteState(TypedDict, total=False):
    chunks: list[str]
    timeoutId: Any


class BaseInputState(TypedDict, total=False):
    """Common properties for input hook results."""

    onInput: Callable[[str, Key], None]
    renderedValue: str
    offset: int
    setOffset: Callable[[int], None]
    # Cursor line (0-indexed) within the rendered text, accounting for wrapping.
    cursorLine: int
    # Cursor column (display-width) within the current line.
    cursorColumn: int
    # Character offset in the full text where the viewport starts (0 when no windowing).
    viewportCharOffset: int
    # Character offset in the full text where the viewport ends (len(text) when no windowing).
    viewportCharEnd: int
    # For paste handling.
    isPasting: bool
    pasteState: _PasteState


# State for text input (alias of BaseInputState).
TextInputState = BaseInputState


class VimInputState(BaseInputState, total=False):
    """State for vim input with mode."""

    mode: VimMode
    setMode: Callable[[VimMode], None]


# ---------------------------------------------------------------------------------------------
# Input modes + queue priorities
# ---------------------------------------------------------------------------------------------

# Input modes for the prompt.
PromptInputMode = Literal[
    "bash",
    "prompt",
    "orphaned-permission",
    "task-notification",
]

# ``Exclude<PromptInputMode, `${string}-notification`>`` — every mode that does not end in
# ``-notification``. Concretely: everything except ``task-notification``.
EditablePromptInputMode = Literal[
    "bash",
    "prompt",
    "orphaned-permission",
]

# Queue priority levels. Same semantics in both normal and proactive mode.
#
#  - ``now``   — Interrupt and send immediately. Aborts any in-flight tool call.
#  - ``next``  — Mid-turn drain. Let the current tool call finish, then send between the tool
#                result and the next API round-trip. Wakes an in-progress SleepTool call.
#  - ``later`` — End-of-turn drain. Wait for the current turn to finish, then process as a new
#                query. Wakes an in-progress SleepTool call.
#
# The SleepTool is only available in proactive mode, so "wakes SleepTool" is a no-op in normal
# mode.
QueuePriority = Literal["now", "next", "later"]


class QueuedCommand(TypedDict, total=False):
    """A queued command (queue entry). Wire keys kept verbatim (camelCase).

    ``value`` and ``mode`` are required in TS; the rest are optional. Per
    ``docs/SPINE_CONTRACTS.md`` this round-trips across the async queue boundary, so its
    sub-payload wire keys (``orphanedPermission`` / ``pastedContents`` / ``preExpansionValue`` /
    ``skipSlashCommands`` / ``isMeta`` / ``agentId``) are preserved exactly.
    """

    value: str | list[ContentBlockParam]  # required
    mode: PromptInputMode  # required
    # Defaults to the priority implied by ``mode`` when enqueued.
    priority: QueuePriority
    uuid: str
    orphanedPermission: OrphanedPermission
    # Raw pasted contents including images. Images are resized at execution time.
    pastedContents: dict[int, PastedContent]
    # The input string before ``[Pasted text #N]`` placeholders were expanded. Used for
    # ultraplan keyword detection. Falls back to ``value`` when unset.
    preExpansionValue: str
    # When ``True``, the input is treated as plain text even if it starts with ``/``.
    skipSlashCommands: bool
    # When ``True``, the resulting UserMessage gets ``isMeta: True`` — hidden in the transcript
    # UI but visible to the model.
    isMeta: bool
    # Provenance of this command. Stamped onto the resulting UserMessage.
    # ``undefined`` = human (keyboard).
    origin: dict[str, Any]
    # Workload tag threaded through to ``cc_workload=`` in the billing-header attribution block.
    workload: str
    # Agent that should receive this notification. ``undefined`` = main thread.
    agentId: AgentId


def is_valid_image_paste(c: PastedContent) -> bool:
    """Type guard for image :data:`PastedContent` with non-empty data.

    Empty-content images (e.g. from a 0-byte file drag) yield empty base64 strings that the API
    rejects with ``image cannot be empty``. Use this at every site that converts ``PastedContent``
    → ``ImageBlockParam`` so the filter and the ID list stay in sync.
    """
    return c.get("type") == "image" and len(c.get("content", "")) > 0


def get_image_paste_ids(
    pasted_contents: dict[int, PastedContent] | None,
) -> list[int] | None:
    """Extract image paste IDs from a :class:`QueuedCommand`'s ``pastedContents``."""
    if not pasted_contents:
        return None
    ids = [c["id"] for c in pasted_contents.values() if is_valid_image_paste(c)]
    return ids if len(ids) > 0 else None


class OrphanedPermission(TypedDict):
    """A permission-result + the assistant message that requested it."""

    permissionResult: PermissionResult
    assistantMessage: AssistantMessage
