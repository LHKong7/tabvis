"""Built-in tool registry, filtering, and MCP tool-pool assembly.

``get_all_base_tools`` is the source of truth for the built-in tools implemented by this
runtime. ``BrowserIntent`` is included but disabled unless its feature flag is enabled. Connected
MCP tools are created dynamically and merged with the enabled built-ins by
:func:`assemble_tool_pool`.
"""

from __future__ import annotations

import os

from tabvis.tool import Tool, Tools

# Import the tool singletons under private aliases so they do NOT shadow the submodule
# attributes of the `tabvis.agent.tools` package (e.g. `from tabvis.agent.tools import grep_tool` must keep
# resolving to the MODULE; the instance lives at `tabvis.agent.tools.grep_tool.grep_tool`).
from tabvis.agent.tools.ask_user_question_tool import ask_user_question_tool as _ask_user_question_tool
from tabvis.agent.tools.bash_tool import bash_tool as _bash_tool
from tabvis.agent.tools.browser_click_tool import browser_click_tool as _browser_click_tool
from tabvis.agent.tools.browser_download_tool import browser_download_tool as _browser_download_tool
from tabvis.agent.tools.browser_intent_tool import browser_intent_tool as _browser_intent_tool
from tabvis.agent.tools.browser_keys_tool import browser_keys_tool as _browser_keys_tool
from tabvis.agent.tools.browser_authenticate_tool import browser_authenticate_tool as _browser_authenticate_tool
from tabvis.agent.tools.browser_navigate_tool import browser_navigate_tool as _browser_navigate_tool
from tabvis.agent.tools.browser_scroll_tool import browser_scroll_tool as _browser_scroll_tool
from tabvis.agent.tools.browser_snapshot_tool import browser_snapshot_tool as _browser_snapshot_tool
from tabvis.agent.tools.browser_type_tool import browser_type_tool as _browser_type_tool
from tabvis.agent.tools.browser_wait_tool import browser_wait_tool as _browser_wait_tool
from tabvis.agent.tools.file_edit_tool import file_edit_tool as _file_edit_tool
from tabvis.agent.tools.file_read_tool import file_read_tool as _file_read_tool
from tabvis.agent.tools.file_write_tool import file_write_tool as _file_write_tool
from tabvis.agent.tools.glob_tool import glob_tool as _glob_tool
from tabvis.agent.tools.grep_tool import grep_tool as _grep_tool
from tabvis.agent.tools.notebook_edit_tool import notebook_edit_tool as _notebook_edit_tool
from tabvis.agent.tools.todo_write_tool import todo_write_tool as _todo_write_tool
from tabvis.agent.tools.tool_search_tool import tool_search_tool as _tool_search_tool
from tabvis.types.permissions import ToolPermissionContext
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.permissions.permissions import get_deny_rule_for_tool


def get_all_base_tools() -> list[Tool]:
    """The exhaustive set of built-in tools available in this environment.

    Dynamic MCP tools are deliberately excluded because they are supplied by connected servers at
    session startup. Features without a working tool implementation, such as WebSearch and plan
    mode, are also not part of this registry.
    """
    # Lazy import: agent_tool pulls in the query loop (tabvis.agent.query → services.tools), which imports
    # tabvis.agent.tools back — importing it here at module load would cycle. skill_tool also imports
    # the command and skill loaders, so it remains lazy-imported.
    # workflow_tool imports tabvis.agent.workflows.run → tabvis.agent.tools.agent_defs/agent_runner, so it is lazy
    # too to keep `tabvis.agent.tools` import-clean.
    from tabvis.agent.tools.agent_tool import agent_tool as agent
    from tabvis.agent.tools.skill_tool import skill_tool as skill
    from tabvis.agent.tools.workflow_tool import workflow_tool as workflow

    return [
        agent,
        skill,
        workflow,
        _bash_tool,
        _glob_tool,
        _grep_tool,
        _file_read_tool,
        _file_edit_tool,
        _file_write_tool,
        _notebook_edit_tool,
        _browser_navigate_tool,
        _browser_snapshot_tool,
        _browser_click_tool,
        _browser_type_tool,
        _browser_scroll_tool,
        _browser_keys_tool,
        _browser_wait_tool,
        _browser_download_tool,
        _browser_intent_tool,  # flag-gated (TABVIS_BROWSER_INTENTS); is_enabled() filters it off by default
        _browser_authenticate_tool,  # flag-gated (TABVIS_AUTHENTICATION_ENABLED); is_enabled() filters it off by default
        _todo_write_tool,
        _ask_user_question_tool,
        _tool_search_tool,
    ]


def get_tools_for_default_preset() -> list[str]:
    return [t.name for t in get_all_base_tools() if t.is_enabled()]


def filter_tools_by_deny_rules(
    tools: Tools, permission_context: ToolPermissionContext
) -> list[Tool]:
    """Drop tools blanket-denied by the permission context."""
    return [t for t in tools if not get_deny_rule_for_tool(permission_context, t)]


def get_tools(permission_context: ToolPermissionContext) -> list[Tool]:
    """Built-in tools after mode/deny/``is_enabled`` filtering."""
    if is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
        # Simple mode: only Bash, Read, and Edit.
        simple_tools = [_bash_tool, _file_read_tool, _file_edit_tool]
        return filter_tools_by_deny_rules(simple_tools, permission_context)

    # (Special tools — ListMcpResources/ReadMcpResource/SyntheticOutput — aren't in the
    # base tool set, so no extra special-tool filtering is needed yet.)
    tools = get_all_base_tools()
    allowed = filter_tools_by_deny_rules(tools, permission_context)
    return [t for t in allowed if t.is_enabled()]


def _uniq_by_name(tools: list[Tool]) -> list[Tool]:
    """Dedupe by name, preserving insertion order (built-ins win on conflict)."""
    seen: set[str] = set()
    out: list[Tool] = []
    for t in tools:
        if t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
    return out


def assemble_tool_pool(
    permission_context: ToolPermissionContext, mcp_tools: Tools
) -> list[Tool]:
    """Combine built-in + MCP tools, sorted per-partition and deduped (built-ins first).

    Built-ins form a contiguous sorted prefix for prompt-cache stability (the server places a
    cache breakpoint after the last built-in); MCP tools are sorted and appended.
    """
    built_in = get_tools(permission_context)
    allowed_mcp = filter_tools_by_deny_rules(mcp_tools, permission_context)
    combined = sorted(built_in, key=lambda t: t.name) + sorted(allowed_mcp, key=lambda t: t.name)
    return _uniq_by_name(combined)


def get_merged_tools(
    permission_context: ToolPermissionContext, mcp_tools: Tools
) -> list[Tool]:
    """Built-in tools followed by MCP tools (no sort/dedup) — for token/search calcs."""
    return [*get_tools(permission_context), *mcp_tools]
