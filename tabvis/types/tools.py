"""Tool progress types

``ToolProgressData`` is a loose tagged dict (``{ kind?, [key]: unknown }``). All the named
progress aliases are the same shape, kept for faithful call-site mapping.
"""

from __future__ import annotations

from typing import Any, TypedDict


class ToolProgressData(TypedDict, total=False):
    kind: str
    # Plus arbitrary extra keys ([key: string]: unknown) — dicts allow these at runtime.


# Tool-specific aliases (all structurally identical to ToolProgressData).
ShellProgress = dict[str, Any]
BashProgress = dict[str, Any]
PowerShellProgress = dict[str, Any]
MCPProgress = dict[str, Any]
SkillToolProgress = dict[str, Any]
TaskOutputProgress = dict[str, Any]
WebSearchProgress = dict[str, Any]
AgentToolProgress = dict[str, Any]
REPLToolProgress = dict[str, Any]
SdkWorkflowProgress = dict[str, Any]
