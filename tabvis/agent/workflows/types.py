"""Workflow type definitions.

Structural types for generated workflow scripts: metadata, agent input/result, per-phase state,
and run options/results. Defined as :class:`typing.TypedDict` classes (camelCase keys kept
verbatim — these are in-memory structures threaded through the workflow runner).
"""

from __future__ import annotations

from typing import Literal, TypedDict

from tabvis.tool import ToolUseContext


class WorkflowMeta(TypedDict, total=False):
    name: str
    description: str


class GeneratedWorkflow(TypedDict):
    meta: WorkflowMeta
    script: str


class WorkflowAgentInput(TypedDict, total=False):
    name: str
    prompt: str  # always expected to be provided; the other fields are optional
    model: str
    agentType: str
    allowedTools: list[str]
    maxTurns: int


class WorkflowAgentResult(TypedDict):
    name: str
    result: str
    totalTokens: int
    toolUses: int
    durationMs: int


class WorkflowPhaseState(TypedDict, total=False):
    name: str
    status: Literal["running", "completed"]
    startedAt: int
    completedAt: int
    agentCount: int
    totalTokens: int
    toolUses: int


class WorkflowRunResult(TypedDict):
    taskId: str
    workflowName: str
    scriptPath: str
    result: str
    totalTokens: int
    toolUses: int
    durationMs: int


class WorkflowRunOptions(TypedDict, total=False):
    taskId: str
    args: str
    script: str
    scriptPath: str
    meta: WorkflowMeta
    context: ToolUseContext


class WorkflowCommandSpec(TypedDict):
    name: str
    description: str
    scriptPath: str
    source: Literal["project", "user"]
    meta: WorkflowMeta
