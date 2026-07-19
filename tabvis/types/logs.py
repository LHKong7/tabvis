"""Session-log / transcript-entry type contracts.

These model the on-disk session transcript: the per-line :class:`Entry` union (transcript
messages plus the various single-purpose metadata records — summaries, titles, tags, PR links,
worktree/attribution/file-history snapshots, queue operations) and the in-memory
:class:`LogOption` describing a discovered log file for the ``/resume`` picker. :func:`sort_logs`
orders those newest-first.

Casing convention: Python identifiers are snake_case; dict-shaped data that round-trips to the
transcript / API keeps its wire keys verbatim. Every transcript record here is a JSON line, so
all of these are :class:`~typing.TypedDict` envelopes (the ``message.py`` style) with their wire
keys unchanged — camelCase (``sessionId`` / ``parentUuid`` / ``leafUuid`` / ``messageId`` /
``customTitle`` / ``aiTitle`` / ``lastPrompt`` / ``timeSavedMs`` / ``agentName`` / ``prNumber`` …)
and the kebab-case ``type`` discriminants (``'custom-title'`` / ``'ai-title'`` / ``'pr-link'`` /
``'worktree-state'`` / ``'content-replacement'`` / ``'file-history-snapshot'`` /
``'attribution-snapshot'`` / ``'speculation-accept'``). No ``extra=forbid`` — the transcript
stays an open record (each entry extends an arbitrary-key base).

``UUID`` is a plain ``str`` on the wire. ``Date`` fields on :class:`LogOption` (``created`` /
``modified``) are real runtime ``datetime`` objects — :func:`sort_logs` compares them directly;
they are not serialized through this module.

``Message`` (``tabvis.types.message``), ``AgentId`` (``tabvis.types.ids``), and
``QueueOperationMessage`` (``tabvis.types.message_queue_types``) are imported at their real
snake_case surface.

Two type-only records — ``FileHistorySnapshot`` and ``ContentReplacementRecord`` — are defined
here as minimal local ``TypedDict`` stubs (verbatim wire keys) so :class:`LogOption` /
:class:`FileHistorySnapshotMessage` / :class:`ContentReplacementEntry` round-trip.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from tabvis.types.ids import AgentId
from tabvis.types.message import Message
from tabvis.types.message_queue_types import QueueOperationMessage

# Re-export the transcript message base. TS ``SerializedMessage = Message & { ... }``; a Python
# ``TypedDict`` can't structurally intersect a union, so :class:`SerializedMessage` is modeled as
# an open record (``total=False``) and ``Message`` is re-exported here so callers can still refer
# to the underlying envelope union at this module's surface (faithful to the TS import).
RawMessage = Message

__all__ = [
    "RawMessage",
    "SerializedMessage",
    "LogOption",
    "SummaryMessage",
    "CustomTitleMessage",
    "AiTitleMessage",
    "LastPromptMessage",
    "TaskSummaryMessage",
    "TagMessage",
    "AgentNameMessage",
    "AgentColorMessage",
    "AgentSettingMessage",
    "PRLinkMessage",
    "PersistedWorktreeSession",
    "WorktreeStateEntry",
    "ContentReplacementEntry",
    "FileHistorySnapshotMessage",
    "FileAttributionState",
    "AttributionSnapshotMessage",
    "TranscriptMessage",
    "SpeculationAcceptMessage",
    "Entry",
    "FileHistorySnapshot",
    "FileHistoryBackup",
    "ContentReplacementRecord",
    "sort_logs",
]


# ============================================================================
# src/utils/toolResultStorage.ts). Minimal faithful TypedDict mirrors — wire keys verbatim.
# ============================================================================


class FileHistoryBackup(TypedDict, total=False):
    """``src/utils/fileHistory.ts`` ``FileHistoryBackup``. ``backupFileName: str | None``."""

    backupFileName: str | None  # null = file did not exist in this version
    version: int
    backupTime: datetime  # TS: Date


class FileHistorySnapshot(TypedDict, total=False):
    """``src/utils/fileHistory.ts`` ``FileHistorySnapshot``."""

    messageId: str  # UUID — the associated message ID for this snapshot
    trackedFileBackups: dict[str, FileHistoryBackup]  # file path -> backup version
    timestamp: datetime  # TS: Date


class ContentReplacementRecord(TypedDict):
    """``src/utils/toolResultStorage.ts`` ``ContentReplacementRecord`` (the tool-result kind)."""

    kind: Literal["tool-result"]
    toolUseId: str
    replacement: str


# ============================================================================
# SerializedMessage / LogOption
# ============================================================================


class SerializedMessage(TypedDict, total=False):
    """A :data:`Message` augmented with persistence metadata (``Message & { cwd, ... }``).

    Open record (extends the ``message.py`` envelope): every :data:`Message` key is also valid.
    """

    cwd: str  # required in practice
    userType: str  # required in practice
    entrypoint: str  # TABVIS_ENTRYPOINT — cli/sdk-ts/sdk-py/etc.
    sessionId: str  # required in practice
    timestamp: str  # required in practice
    version: str  # required in practice
    gitBranch: str
    slug: str  # session slug for files like plans (used for resume)


class LogOption(TypedDict, total=False):
    """A discovered session-log file, as surfaced to the ``/resume`` picker."""

    date: str  # required in practice
    messages: list[SerializedMessage]  # required in practice
    fullPath: str
    value: int  # required in practice
    created: datetime  # TS: Date
    modified: datetime  # TS: Date
    firstPrompt: str  # required in practice
    messageCount: int  # required in practice
    fileSize: int  # bytes (for display)
    isSidechain: bool  # required in practice
    isLite: bool  # true for lite logs (messages not loaded)
    sessionId: str  # session ID for lite logs
    teamName: str  # team name if this is a spawned agent session
    agentName: str  # agent's custom name (from /rename or swarm)
    agentColor: str  # agent's color (from /rename or swarm)
    agentSetting: str  # agent definition used (--agent flag or settings.agent)
    isTeammate: bool  # whether created by a swarm teammate
    leafUuid: str  # UUID — if given, must appear in the DB
    summary: str  # optional conversation summary
    customTitle: str  # optional user-set custom title
    tag: str  # optional searchable tag
    fileHistorySnapshots: list[FileHistorySnapshot]
    attributionSnapshots: list[AttributionSnapshotMessage]
    gitBranch: str  # git branch at the end of the session
    projectPath: str  # original project directory path
    prNumber: int  # GitHub PR number linked to this session
    prUrl: str  # full URL to the linked PR
    prRepository: str  # "owner/repo"
    worktreeSession: PersistedWorktreeSession | None  # null = exited, absent = never entered
    contentReplacements: list[ContentReplacementRecord]


# ============================================================================
# Single-purpose metadata records (transcript Entry members)
# ============================================================================


class SummaryMessage(TypedDict):
    type: Literal["summary"]
    leafUuid: str  # UUID
    summary: str


class CustomTitleMessage(TypedDict):
    type: Literal["custom-title"]
    sessionId: str  # UUID
    customTitle: str


class AiTitleMessage(TypedDict):
    """AI-generated session title. Distinct from :class:`CustomTitleMessage` so user renames
    always win and ``reAppendSessionMetadata`` never re-appends ephemeral AI titles."""

    type: Literal["ai-title"]
    sessionId: str  # UUID
    aiTitle: str


class LastPromptMessage(TypedDict):
    type: Literal["last-prompt"]
    sessionId: str  # UUID
    lastPrompt: str


class TaskSummaryMessage(TypedDict):
    """Periodic fork-generated summary of what the agent is currently doing (for ``tabvis ps``)."""

    type: Literal["task-summary"]
    sessionId: str  # UUID
    summary: str
    timestamp: str


class TagMessage(TypedDict):
    type: Literal["tag"]
    sessionId: str  # UUID
    tag: str


class AgentNameMessage(TypedDict):
    type: Literal["agent-name"]
    sessionId: str  # UUID
    agentName: str


class AgentColorMessage(TypedDict):
    type: Literal["agent-color"]
    sessionId: str  # UUID
    agentColor: str


class AgentSettingMessage(TypedDict):
    type: Literal["agent-setting"]
    sessionId: str  # UUID
    agentSetting: str


class PRLinkMessage(TypedDict):
    """PR-link message: associates a session with a GitHub pull request."""

    type: Literal["pr-link"]
    sessionId: str  # UUID
    prNumber: int
    prUrl: str
    prRepository: str  # e.g. "owner/repo"
    timestamp: str  # ISO timestamp when linked


class PersistedWorktreeSession(TypedDict, total=False):
    """Worktree session state persisted to the transcript for resume (subset of
    ``WorktreeSession`` — excludes ephemeral first-run-analytics fields)."""

    originalCwd: str  # required in practice
    worktreePath: str  # required in practice
    worktreeName: str  # required in practice
    worktreeBranch: str
    originalBranch: str
    originalHeadCommit: str
    sessionId: str  # required in practice
    tmuxSessionName: str
    hookBased: bool


class WorktreeStateEntry(TypedDict):
    """Records whether the session is currently inside a worktree (last-wins; null = exited)."""

    type: Literal["worktree-state"]
    sessionId: str  # UUID
    worktreeSession: PersistedWorktreeSession | None


class ContentReplacementEntry(TypedDict, total=False):
    """Records content blocks whose in-context representation was replaced with a stub.

    ``agentId`` present => a subagent sidechain record; absent => main-thread (``/resume``).
    """

    type: Literal["content-replacement"]  # required
    sessionId: str  # UUID — required
    agentId: AgentId  # optional
    replacements: list[ContentReplacementRecord]  # required


class FileHistorySnapshotMessage(TypedDict):
    type: Literal["file-history-snapshot"]
    messageId: str  # UUID
    snapshot: FileHistorySnapshot
    isSnapshotUpdate: bool


class FileAttributionState(TypedDict):
    """Per-file attribution state tracking Tabvis's character contributions."""

    contentHash: str  # SHA-256 hash of file content
    tabvisContribution: int  # characters written by Tabvis
    mtime: int  # file modification time


class AttributionSnapshotMessage(TypedDict, total=False):
    """Attribution snapshot: tracks character-level Tabvis contributions for commit attribution."""

    type: Literal["attribution-snapshot"]  # required
    messageId: str  # UUID — required
    surface: str  # required — client surface (cli, ide, web, api)
    fileStates: dict[str, FileAttributionState]  # required
    promptCount: int  # total prompts in session
    promptCountAtLastCommit: int  # prompts at last commit
    permissionPromptCount: int  # total permission prompts shown
    permissionPromptCountAtLastCommit: int  # permission prompts at last commit
    escapeCount: int  # total ESC presses (cancelled permission prompts)
    escapeCountAtLastCommit: int  # ESC presses at last commit


class TranscriptMessage(SerializedMessage, total=False):
    """A :class:`SerializedMessage` with transcript-link fields (``parentUuid`` etc.)."""

    parentUuid: str | None  # UUID | null — required
    logicalParentUuid: str | None  # preserves logical parent when parentUuid is nullified
    isSidechain: bool  # required
    gitBranch: str
    agentId: str  # agent ID for sidechain transcripts (resume)
    teamName: str  # team name if this is a spawned agent session
    agentName: str  # agent's custom name (from /rename or swarm)
    agentColor: str  # agent's color (from /rename or swarm)
    promptId: str  # correlates with OTel prompt.id for user prompt messages


class SpeculationAcceptMessage(TypedDict):
    type: Literal["speculation-accept"]
    timestamp: str
    timeSavedMs: int


# ============================================================================
# Entry union
# ============================================================================

Entry = (
    TranscriptMessage
    | SummaryMessage
    | CustomTitleMessage
    | AiTitleMessage
    | LastPromptMessage
    | TaskSummaryMessage
    | TagMessage
    | AgentNameMessage
    | AgentColorMessage
    | AgentSettingMessage
    | PRLinkMessage
    | FileHistorySnapshotMessage
    | AttributionSnapshotMessage
    | QueueOperationMessage
    | SpeculationAcceptMessage
    | WorktreeStateEntry
    | ContentReplacementEntry
)


# ============================================================================
# sortLogs
# ============================================================================


def sort_logs(logs: list[LogOption]) -> list[LogOption]:
    """Sort logs newest-first by ``modified`` date, tie-breaking on ``created`` (also newest-first).

    Faithful to TS ``sortLogs``: an in-place ``Array.prototype.sort`` that returns the same array.
    Python's ``list.sort`` is stable and likewise in-place; the comparator is expressed as a
    ``(modified, created)`` descending sort key (equivalent to ``b - a`` on each ``.getTime()``).
    """
    logs.sort(key=lambda log: (log["modified"], log["created"]), reverse=True)
    return logs
