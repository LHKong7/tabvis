"""Agent tool-availability sets.

Source of truth for which tools each agent flavour may use:

* :data:`ALL_AGENT_DISALLOWED_TOOLS` / :data:`CUSTOM_AGENT_DISALLOWED_TOOLS` —
  blocked for *all* / custom agents.
* :data:`ASYNC_AGENT_ALLOWED_TOOLS` — the allow-set for async agents.
* :data:`IN_PROCESS_TEAMMATE_ALLOWED_TOOLS` — extra tools for in-process teammates.

Implementation notes
--------------------
Each ``*_TOOL_NAME`` constant is inlined here as a plain string literal rather than imported
from its owning tool module. In the flat ``tabvis/tools/`` layout those modules are heavy
(``agent_tool`` pulls in the whole query loop, ``task_*`` pull in the task runtime, etc.), so
importing them at module load would create import cycles and buys nothing for what are plain
string literals. Each inlined value is annotated with its source module and kept in sync with
the value declared there.

``USER_TYPE`` is read from the environment at import time — re-import the module (or recompute
the set) if the env changes.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Tool-name constants (inlined string literals; source module noted inline).
# ---------------------------------------------------------------------------
TASK_OUTPUT_TOOL_NAME = "TaskOutput"  # tabvis.agent.tools.task_output_tool
EXIT_PLAN_MODE_V2_TOOL_NAME = "ExitPlanMode"  # tabvis.agent.tools.exit_plan_mode_tool
ENTER_PLAN_MODE_TOOL_NAME = "EnterPlanMode"  # tabvis.agent.tools.enter_plan_mode_tool
AGENT_TOOL_NAME = "Agent"  # tabvis.agent.tools.agent_tool
ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"  # tabvis.agent.tools.ask_user_question_tool
TASK_STOP_TOOL_NAME = "TaskStop"  # tabvis.agent.tools.task_stop_tool
FILE_READ_TOOL_NAME = "Read"  # tabvis.agent.tools.file_read_tool
WEB_SEARCH_TOOL_NAME = "WebSearch"  # tabvis.agent.tools.web_search_tool
TODO_WRITE_TOOL_NAME = "TodoWrite"  # tabvis.agent.tools.todo_write_tool
GREP_TOOL_NAME = "Grep"  # tabvis.agent.tools.grep_tool
WEB_FETCH_TOOL_NAME = "WebFetch"  # REMOVED as a tool; the name survives only for
#                                  legacy filters (compaction, sandbox rules).
GLOB_TOOL_NAME = "Glob"  # tabvis.agent.tools.glob_tool
FILE_EDIT_TOOL_NAME = "Edit"  # tabvis.agent.tools.file_edit_tool
FILE_WRITE_TOOL_NAME = "Write"  # tabvis.agent.tools.file_write_tool
NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"  # tabvis.agent.tools.notebook_edit_tool
SKILL_TOOL_NAME = "Skill"  # tabvis.agent.tools.skill_tool
SEND_MESSAGE_TOOL_NAME = "SendMessage"  # tabvis.agent.tools.send_message_tool_constants
TASK_CREATE_TOOL_NAME = "TaskCreate"  # tabvis.agent.tools.task_create_tool
TASK_GET_TOOL_NAME = "TaskGet"  # tabvis.agent.tools.task_get_tool
TASK_LIST_TOOL_NAME = "TaskList"  # tabvis.agent.tools.task_list_tool
TASK_UPDATE_TOOL_NAME = "TaskUpdate"  # tabvis.agent.tools.task_update_tool
TOOL_SEARCH_TOOL_NAME = "ToolSearch"  # tabvis.agent.tools.tool_search_tool
SYNTHETIC_OUTPUT_TOOL_NAME = "StructuredOutput"  # tabvis.agent.tools.synthetic_output_tool_synthetic_output_tool  # noqa: E501
ENTER_WORKTREE_TOOL_NAME = "EnterWorktree"  # tabvis.agent.tools.enter_worktree_tool_constants
EXIT_WORKTREE_TOOL_NAME = "ExitWorktree"  # tabvis.agent.tools.exit_worktree_tool_constants
BROWSER_NAVIGATE_TOOL_NAME = "BrowserNavigate"  # tabvis.agent.tools.browser_navigate_tool
BROWSER_SNAPSHOT_TOOL_NAME = "BrowserSnapshot"  # tabvis.agent.tools.browser_snapshot_tool
BROWSER_CLICK_TOOL_NAME = "BrowserClick"  # tabvis.agent.tools.browser_click_tool
BROWSER_TYPE_TOOL_NAME = "BrowserType"  # tabvis.agent.tools.browser_type_tool
BROWSER_WAIT_TOOL_NAME = "BrowserWait"  # tabvis.agent.tools.browser_wait_tool
BROWSER_DOWNLOAD_TOOL_NAME = "BrowserDownload"  # tabvis.agent.tools.browser_download_tool
BROWSER_INTENT_TOOL_NAME = "BrowserIntent"  # tabvis.agent.tools.browser_intent_tool (flag-gated: TABVIS_BROWSER_INTENTS)

# ``SHELL_TOOL_NAMES`` (utils/shell/shellToolUtils): [BASH_TOOL_NAME, POWERSHELL_TOOL_NAME].
# Inlined to keep this module standalone (the shell util pulls bash/powershell tool names).
SHELL_TOOL_NAMES: list[str] = ["Bash", "PowerShell"]


ALL_AGENT_DISALLOWED_TOOLS: set[str] = {
    TASK_OUTPUT_TOOL_NAME,
    EXIT_PLAN_MODE_V2_TOOL_NAME,
    ENTER_PLAN_MODE_TOOL_NAME,
    # Allow Agent tool for agents when user is ant (enables nested agents)
    *[AGENT_TOOL_NAME],
    ASK_USER_QUESTION_TOOL_NAME,
    TASK_STOP_TOOL_NAME,
}

CUSTOM_AGENT_DISALLOWED_TOOLS: set[str] = {
    *ALL_AGENT_DISALLOWED_TOOLS,
}

#
# Async Agent Tool Availability Status (Source of Truth)
#
ASYNC_AGENT_ALLOWED_TOOLS: set[str] = {
    FILE_READ_TOOL_NAME,
    WEB_SEARCH_TOOL_NAME,
    TODO_WRITE_TOOL_NAME,
    GREP_TOOL_NAME,
    GLOB_TOOL_NAME,
    *SHELL_TOOL_NAMES,
    FILE_EDIT_TOOL_NAME,
    FILE_WRITE_TOOL_NAME,
    NOTEBOOK_EDIT_TOOL_NAME,
    SKILL_TOOL_NAME,
    SYNTHETIC_OUTPUT_TOOL_NAME,
    TOOL_SEARCH_TOOL_NAME,
    ENTER_WORKTREE_TOOL_NAME,
    EXIT_WORKTREE_TOOL_NAME,
}

# Tools allowed only for in-process teammates (not general async agents).
# These are injected by the in-process runner and allowed through agent tool
# filtering via an is-in-process-teammate check.
IN_PROCESS_TEAMMATE_ALLOWED_TOOLS: set[str] = {
    TASK_CREATE_TOOL_NAME,
    TASK_GET_TOOL_NAME,
    TASK_LIST_TOOL_NAME,
    TASK_UPDATE_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
}

# BLOCKED FOR ASYNC AGENTS:
# - AgentTool: Blocked to prevent recursion
# - TaskOutputTool: Blocked to prevent recursion
# - ExitPlanModeTool: Plan mode is a main thread abstraction.
# - TaskStopTool: Requires access to main thread task state.
# - TungstenTool: Uses singleton virtual terminal abstraction that conflicts between agents.
#
# ENABLE LATER (NEED WORK):
# - MCPTool: TBD
# - ListMcpResourcesTool: TBD
# - ReadMcpResourceTool: TBD
