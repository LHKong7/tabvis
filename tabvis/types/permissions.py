"""Permission type definitions

Pure type definitions and constants with no runtime dependencies, extracted to break
import cycles. Tagged-union decisions are modeled as ``TypedDict`` variants keyed by
``behavior``/``type`` (faithful to the TS discriminated unions).
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

# ============================================================================
# Permission Modes
# ============================================================================

EXTERNAL_PERMISSION_MODES: tuple[str, ...] = (
    "acceptEdits",
    "bypassPermissions",
    "default",
    "dontAsk",
    "plan",
)

ExternalPermissionMode = Literal[
    "acceptEdits", "bypassPermissions", "default", "dontAsk", "plan"
]
# Exhaustive mode union (adds the internal 'bubble' mode).
InternalPermissionMode = ExternalPermissionMode | Literal["bubble"]
PermissionMode = InternalPermissionMode

INTERNAL_PERMISSION_MODES: tuple[str, ...] = EXTERNAL_PERMISSION_MODES
PERMISSION_MODES: tuple[str, ...] = INTERNAL_PERMISSION_MODES

# ============================================================================
# Permission Behaviors
# ============================================================================

PermissionBehavior = Literal["allow", "deny", "ask"]

# ============================================================================
# Permission Rules
# ============================================================================

PermissionRuleSource = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "flagSettings",
    "policySettings",
    "cliArg",
    "command",
    "session",
]


class PermissionRuleValue(TypedDict, total=False):
    toolName: str  # required in practice
    ruleContent: str


class PermissionRule(TypedDict):
    source: PermissionRuleSource
    ruleBehavior: PermissionBehavior
    ruleValue: PermissionRuleValue


# ============================================================================
# Permission Updates
# ============================================================================

PermissionUpdateDestination = Literal[
    "userSettings", "projectSettings", "localSettings", "session", "cliArg"
]

# PermissionUpdate is a tagged union (type: addRules|replaceRules|removeRules|setMode|
# addDirectories|removeDirectories). Modeled loosely as a dict for now.
PermissionUpdate = dict[str, Any]

WorkingDirectorySource = PermissionRuleSource


class AdditionalWorkingDirectory(TypedDict):
    path: str
    source: WorkingDirectorySource


# ============================================================================
# Permission Decisions & Results
# ============================================================================


class PermissionCommandMetadata(TypedDict, total=False):
    name: str  # required in practice
    description: str
    # Plus arbitrary extra keys for forward compatibility.


# PermissionMetadata = { command: PermissionCommandMetadata } | None
PermissionMetadata = dict[str, Any] | None


class PermissionAllowDecision(TypedDict, total=False):
    behavior: Literal["allow"]  # required
    updatedInput: dict[str, Any]
    userModified: bool
    decisionReason: PermissionDecisionReason
    toolUseID: str
    acceptFeedback: str
    contentBlocks: list[Any]


class PermissionAskDecision(TypedDict, total=False):
    behavior: Literal["ask"]  # required
    message: str  # required
    updatedInput: dict[str, Any]
    decisionReason: PermissionDecisionReason
    suggestions: list[PermissionUpdate]
    blockedPath: str
    metadata: PermissionMetadata
    isBashSecurityCheckForMisparsing: bool
    contentBlocks: list[Any]


class PermissionDenyDecision(TypedDict, total=False):
    behavior: Literal["deny"]  # required
    message: str  # required
    decisionReason: PermissionDecisionReason  # required
    toolUseID: str


PermissionDecision = (
    PermissionAllowDecision | PermissionAskDecision | PermissionDenyDecision
)


class _PermissionPassthrough(TypedDict, total=False):
    behavior: Literal["passthrough"]  # required
    message: str  # required
    decisionReason: Any
    suggestions: list[PermissionUpdate]
    blockedPath: str


PermissionResult = PermissionDecision | _PermissionPassthrough

# PermissionDecisionReason is a tagged union ({type: 'rule'|'mode'|...}). Loose dict for now.
PermissionDecisionReason = dict[str, Any]

# ============================================================================
# Permission Explainer Types
# ============================================================================

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]


class PermissionExplanation(TypedDict):
    riskLevel: RiskLevel
    explanation: str
    reasoning: str
    risk: str


# ============================================================================
# Tool Permission Context
# ============================================================================

# Mapping of permission rules by their source: { [source]?: list[str] }
ToolPermissionRulesBySource = dict[str, list[str]]


class ToolPermissionContext(TypedDict, total=False):
    mode: PermissionMode  # required in practice
    additionalWorkingDirectories: dict[str, AdditionalWorkingDirectory]  # required
    alwaysAllowRules: ToolPermissionRulesBySource  # required
    alwaysDenyRules: ToolPermissionRulesBySource  # required
    alwaysAskRules: ToolPermissionRulesBySource  # required
    isBypassPermissionsModeAvailable: bool  # required
    strippedDangerousRules: ToolPermissionRulesBySource
    shouldAvoidPermissionPrompts: bool
    awaitAutomatedChecksBeforeDialog: bool
    prePlanMode: PermissionMode
