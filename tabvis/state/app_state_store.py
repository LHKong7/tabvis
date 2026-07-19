"""App state store

Headless slice only: the spine + tools read ``toolPermissionContext``, ``mainLoopModel``,
``mcp``, ``agentDefinitions``, ``todos``/``tasks``, ``thinkingEnabled``, ``verbose``,
``settings``. The large UI surface (footer/tungsten/bagel/repl/team/companion/speculation…)
is intentionally omitted. AppState is a **plain dict with camelCase keys** (mirrors the TS
object; tools read e.g. ``app_state["toolPermissionContext"]``).
"""

from __future__ import annotations

from typing import Any, TypedDict

from tabvis.state.store import Store, create_store
from tabvis.tool import get_empty_tool_permission_context
from tabvis.types.permissions import ToolPermissionContext
from tabvis.utils.thinking import should_enable_thinking_by_default


class McpState(TypedDict):
    clients: list[Any]
    tools: list[Any]
    commands: list[Any]
    resources: dict[str, Any]


class AppState(TypedDict, total=False):
    settings: dict[str, Any]
    verbose: bool
    mainLoopModel: str | None
    mainLoopModelForSession: str | None
    toolPermissionContext: ToolPermissionContext
    agent: str | None
    agentDefinitions: dict[str, Any]
    fileHistory: dict[str, Any]
    attribution: dict[str, Any]
    mcp: McpState
    todos: dict[str, Any]
    tasks: dict[str, Any]
    thinkingEnabled: bool | None
    # Compact, JSON-safe view of the browser this agent is driving (never a live Playwright
    # handle — the real objects live on the BrowserService singleton). See
    # tabvis.browser.session.BrowserSessionRecord.summary().
    browserSession: dict[str, Any]


AppStateStore = Store


def get_default_app_state() -> AppState:
    # Skeleton: no teammate/plan-mode-required initialization -> 'default' mode.
    initial_mode = "default"
    permission_context: ToolPermissionContext = {
        **get_empty_tool_permission_context(),
        "mode": initial_mode,
    }
    return {
        # Intentionally {} — do NOT wire get_initial_settings() directly: it returns a SettingsJson
        # model, but on_change_app_state reads app_state["settings"] as a plain dict (.get("env")),
        # so a model here would regress that path. Wiring needs a dict projection first.
        "settings": {},
        "verbose": False,
        "mainLoopModel": None,  # alias/full-name/None (default)
        "mainLoopModelForSession": None,
        "toolPermissionContext": permission_context,
        "agent": None,
        "agentDefinitions": {"activeAgents": [], "allAgents": []},
        "fileHistory": {"snapshots": [], "trackedFiles": set(), "snapshotSequence": 0},
        # Intentionally {} — do NOT wire create_empty_attribution_state() directly: it returns an
        # AttributionState dataclass, but tabvis.utils.attribution reads
        # app_state["attribution"]["fileStates"] and treats {} as "disabled"; a populated dataclass
        # would flip it on and crash that subscript. Attribution stays dormant in headless.
        "attribution": {},
        "mcp": {"clients": [], "tools": [], "commands": [], "resources": {}},
        "todos": {},
        "tasks": {},
        "thinkingEnabled": should_enable_thinking_by_default(),
        "browserSession": {},
    }


def create_app_state_store(on_change=None) -> Store:
    return create_store(get_default_app_state(), on_change)
