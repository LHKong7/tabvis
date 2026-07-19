"""Agent (subagent) definitions.

Defines the built-in subagents available to the Agent tool:

* :data:`GENERAL_PURPOSE_AGENT` — general-purpose research/search/multi-step-task agent.
* :data:`STATUSLINE_SETUP_AGENT` — configures the user's status line.
* :data:`TABVIS_GUIDE_AGENT` — answers questions about Tabvis itself (non-embedded-search branch;
  the dynamic context sections — custom skills/agents/MCP/settings — are clean-env empty here so
  the base prompt is returned as-is).
* :data:`EXPLORE_AGENT` / :data:`PLAN_AGENT` / :data:`VERIFICATION_AGENT` — codebase exploration,
  implementation planning, and post-implementation verification agents (all currently dead-gated
  out of the active set).
* :func:`get_builtin_agents` (full gate logic) + :func:`are_explore_plan_agents_enabled` (always
  ``False`` in this build).
* :func:`load_agents_dir` (a *small* scanner of ``<cwd>/.tabvis/agents/*.md`` and
  ``~/.tabvis/agents/*.md`` with YAML frontmatter) and :func:`get_agent_definitions_with_overrides`
  (built-ins merged with dir agents, dir agents overriding built-ins by ``agent_type`` — the
  ``AgentDefinitionsResult`` shape).

Out of scope here: the full upward ``.tabvis`` config walk (managed/policy/worktree/inode-dedup),
JSON agents, MCP-server specs, hooks, skills, memory scope, effort, permission mode, isolation,
color manager. The dataclass keeps only the fields the runner needs (``agent_type`` /
``when_to_use`` / ``tools`` / ``source`` / ``base_dir`` / ``model`` / ``get_system_prompt``).

Casing: Python identifiers are snake_case; the ``AgentDefinitionsResult`` dict keeps camelCase
wire keys (``activeAgents`` / ``allAgents``) so it round-trips through ``ToolUseContextOptions``
and the SDK ``init`` message unchanged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import get_tabvis_config_home_dir, is_env_truthy

__all__ = [
    "TABVIS_GUIDE_AGENT",
    "TABVIS_GUIDE_AGENT_TYPE",
    "EXPLORE_AGENT",
    "EXPLORE_AGENT_MIN_QUERIES",
    "GENERAL_PURPOSE_AGENT",
    "PLAN_AGENT",
    "RED_TEAM_AGENT",
    "STATUSLINE_SETUP_AGENT",
    "VERIFICATION_AGENT",
    "AgentDefinition",
    "are_explore_plan_agents_enabled",
    "get_agent_definitions_with_overrides",
    "get_builtin_agents",
    "load_agents_dir",
]


# --------------------------------------------------------------------------------------------
# AgentDefinition
# --------------------------------------------------------------------------------------------


@dataclass
class AgentDefinition:
    """A subagent definition.

    ``tools`` is ``["*"]`` for "all tools", a concrete allowlist of tool names, or ``None``
    (also treated as "all tools" — an omitted field means all tools). ``model`` is ``None`` to
    inherit the parent's main-loop model. ``get_system_prompt`` is a zero-arg callable returning
    the agent's system prompt string; it takes no context argument since the general-purpose
    prompt is static.
    """

    agent_type: str
    when_to_use: str  # ``whenToUse`` / the agent ``description``.
    get_system_prompt: Callable[[], str]
    tools: list[str] | None = field(default_factory=lambda: ["*"])
    source: str = "built-in"
    base_dir: str = "built-in"
    model: str | None = None
    # Optional built-in-agent fields (statusline-setup / tabvis-guide / Explore / Plan / verification).
    # ``disallowed_tools`` is the inverse of ``tools`` (a blocklist), ``permission_mode`` gates the
    # subagent's permission prompts, ``color`` is the UI badge color, ``background`` runs the agent
    # off the main loop, ``omit_tabvis_md`` drops TABVIS.md from the subagent context, and
    # ``critical_system_reminder`` is an EXPERIMENTAL extra system-reminder line.
    disallowed_tools: list[str] | None = None
    permission_mode: str | None = None
    color: str | None = None
    background: bool = False
    omit_tabvis_md: bool = False
    critical_system_reminder: str | None = None


# --------------------------------------------------------------------------------------------
# GENERAL_PURPOSE_AGENT
# --------------------------------------------------------------------------------------------

# Note: absolute-path + emoji guidance would be appended by a separate env-details enhancement
# step, which is not implemented in this build.
_SHARED_PREFIX = (
    "You are an agent for Tabvis, Provider's official CLI for Tabvis. Given the user's message, "
    "you should use the tools available to complete the task. Complete the task fully—don't "
    "gold-plate, but don't leave it half-done."
)

_SHARED_GUIDELINES = """Your strengths:
- Searching for code, configurations, and patterns across large codebases
- Analyzing multiple files to understand system architecture
- Investigating complex questions that require exploring many files
- Performing multi-step research tasks

Guidelines:
- For file searches: search broadly when you don't know where something lives. Use Read when you know the specific file path.
- For analysis: Start broad and narrow down. Use multiple search strategies if the first doesn't yield results.
- Be thorough: Check multiple locations, consider different naming conventions, look for related files.
- NEVER create files unless they're absolutely necessary for achieving your goal. ALWAYS prefer editing an existing file to creating a new one.
- NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested."""  # noqa: E501


def _get_general_purpose_system_prompt() -> str:
    """Build the general-purpose agent's system prompt."""
    return (
        f"{_SHARED_PREFIX} When you complete the task, respond with a concise report covering "
        "what was done and any key findings — the caller will relay this to the user, so it "
        "only needs the essentials.\n\n"
        f"{_SHARED_GUIDELINES}"
    )


GENERAL_PURPOSE_AGENT = AgentDefinition(
    agent_type="general-purpose",
    when_to_use=(
        "General-purpose agent for researching complex questions, searching for code, and "
        "executing multi-step tasks. When you are searching for a keyword or file and are not "
        "confident that you will find the right match in the first few tries use this agent to "
        "perform the search for you."
    ),
    tools=["*"],
    source="built-in",
    base_dir="built-in",
    # model is intentionally None — inherits the parent's main-loop model (getDefaultSubagentModel).
    get_system_prompt=_get_general_purpose_system_prompt,
)


# --------------------------------------------------------------------------------------------
# Tool-name constants + embedded-search gate (inlined, matching grep_tool.py's pattern)
# --------------------------------------------------------------------------------------------

# Inlined here to avoid importing the (heavy) tool modules + the query-loop import cycle — same
# approach grep_tool.py already takes for AGENT/BASH names.
_BASH_TOOL_NAME = "Bash"
_FILE_READ_TOOL_NAME = "Read"
_FILE_EDIT_TOOL_NAME = "Edit"
_FILE_WRITE_TOOL_NAME = "Write"
_GLOB_TOOL_NAME = "Glob"
_GREP_TOOL_NAME = "Grep"
_NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"
_EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"
_BROWSER_NAVIGATE_TOOL_NAME = "BrowserNavigate"
_BROWSER_SNAPSHOT_TOOL_NAME = "BrowserSnapshot"
_WEB_SEARCH_TOOL_NAME = "WebSearch"
_SEND_MESSAGE_TOOL_NAME = "SendMessage"
_AGENT_TOOL_NAME = "Agent"


def _has_embedded_search_tools() -> bool:
    """Whether embedded search tools (bfs/ugrep aliased to find/grep) are active.

    The gate also requires ``TABVIS_ENTRYPOINT`` to be a non-SDK / non-local-agent entrypoint. In a
    clean env ``EMBEDDED_SEARCH_TOOLS`` is unset, so this is ``False`` and every built-in prompt
    below resolves to the **non-embedded-search branch** (the dedicated Glob/Grep tool names),
    which is the branch this build targets.
    """
    if not is_env_truthy(os.environ.get("EMBEDDED_SEARCH_TOOLS")):
        return False
    entrypoint = os.environ.get("TABVIS_ENTRYPOINT")
    return entrypoint not in ("sdk-ts", "sdk-py", "sdk-cli", "local-agent")


def _is_using_3p_services() -> bool:
    """Whether third-party (Foundry) services are configured: ``is_env_truthy(TABVIS_USE_FOUNDRY)``."""
    return is_env_truthy(os.environ.get("TABVIS_USE_FOUNDRY"))


def _get_is_non_interactive_session() -> bool:
    """Whether the current session is non-interactive.

    The headless ``tabvis -p`` spine is always non-interactive (``query_engine`` threads
    ``is_non_interactive_session=True``), so this always returns ``True``.
    """
    return True


# --------------------------------------------------------------------------------------------
# STATUSLINE_SETUP_AGENT
# --------------------------------------------------------------------------------------------

# NB: the intended prompt text carries trailing whitespace on four lines (a ``~/.bashrc`` line,
# the ``$(hostname -s)`` line, one blank line, and the ``"type": "command",`` line). Editors/ruff
# strip trailing whitespace, so the body below is kept clean and the exact trailing whitespace is
# re-appended by ``_restore_statusline_trailing_ws`` to keep the prompt text byte-identical to its
# intended form.
_STATUSLINE_SYSTEM_PROMPT_CLEAN = r"""You are a status line setup agent for Tabvis. Your job is to create or update the statusLine command in the user's Tabvis settings.

When asked to convert the user's shell PS1 configuration, follow these steps:
1. Read the user's shell configuration files in this order of preference:
   - ~/.zshrc
   - ~/.bashrc
   - ~/.bash_profile
   - ~/.profile

2. Extract the PS1 value using this regex pattern: /(?:^|\n)\s*(?:export\s+)?PS1\s*=\s*["']([^"']+)["']/m

3. Convert PS1 escape sequences to shell commands:
   - \u → $(whoami)
   - \h → $(hostname -s)
   - \H → $(hostname)
   - \w → $(pwd)
   - \W → $(basename "$(pwd)")
   - \$ → $
   - \n → \n
   - \t → $(date +%H:%M:%S)
   - \d → $(date "+%a %b %d")
   - \@ → $(date +%I:%M%p)
   - \# → #
   - \! → !

4. When using ANSI color codes, be sure to use `printf`. Do not remove colors. Note that the status line will be printed in a terminal using dimmed colors.

5. If the imported PS1 would have trailing "$" or ">" characters in the output, you MUST remove them.

6. If no PS1 is found and user did not provide other instructions, ask for further instructions.

How to use the statusLine command:
1. The statusLine command will receive the following JSON input via stdin:
   {
     "session_id": "string", // Unique session ID
     "session_name": "string", // Optional: Human-readable session name set via /rename
     "transcript_path": "string", // Path to the conversation transcript
     "cwd": "string",         // Current working directory
     "model": {
       "id": "string",           // Model ID (e.g., "claude-3-5-sonnet-20241022")
       "display_name": "string"  // Display name (e.g., "TABVIS Balanced 3.5")
     },
     "workspace": {
       "current_dir": "string",  // Current working directory path
       "project_dir": "string",  // Project root directory path
       "added_dirs": ["string"]  // Directories added via /add-dir
     },
     "version": "string",        // Tabvis app version (e.g., "1.0.71")
     "output_style": {
       "name": "string",         // Output style name (e.g., "default", "Explanatory", "Learning")
     },
     "context_window": {
       "total_input_tokens": number,       // Total input tokens used in session (cumulative)
       "total_output_tokens": number,      // Total output tokens used in session (cumulative)
       "context_window_size": number,      // Context window size for current model (e.g., 200000)
       "current_usage": {                   // Token usage from last API call (null if no messages yet)
         "input_tokens": number,           // Input tokens for current context
         "output_tokens": number,          // Output tokens generated
         "cache_creation_input_tokens": number,  // Tokens written to cache
         "cache_read_input_tokens": number       // Tokens read from cache
       } | null,
       "used_percentage": number | null,      // Pre-calculated: % of context used (0-100), null if no messages yet
       "remaining_percentage": number | null  // Pre-calculated: % of context remaining (0-100), null if no messages yet
     },
     "rate_limits": {             // Optional API rate limit windows from the latest response.
       "five_hour": {             // Optional: 5-hour session limit (may be absent)
         "used_percentage": number,   // Percentage of limit used (0-100)
         "resets_at": number          // Unix epoch seconds when this window resets
       },
       "seven_day": {             // Optional: 7-day weekly limit (may be absent)
         "used_percentage": number,   // Percentage of limit used (0-100)
         "resets_at": number          // Unix epoch seconds when this window resets
       }
     },
     "vim": {                     // Optional, only present when vim mode is enabled
       "mode": "INSERT" | "NORMAL"  // Current vim editor mode
     },
     "agent": {                    // Optional, only present when Tabvis is started with --agent flag
       "name": "string",           // Agent name (e.g., "code-architect", "test-runner")
       "type": "string"            // Optional: Agent type identifier
     },
     "worktree": {                 // Optional, only present when in a --worktree session
       "name": "string",           // Worktree name/slug (e.g., "my-feature")
       "path": "string",           // Full path to the worktree directory
       "branch": "string",         // Optional: Git branch name for the worktree
       "original_cwd": "string",   // The directory Tabvis was in before entering the worktree
       "original_branch": "string" // Optional: Branch that was checked out before entering the worktree
     }
   }

   You can use this JSON data in your command like:
   - $(cat | jq -r '.model.display_name')
   - $(cat | jq -r '.workspace.current_dir')
   - $(cat | jq -r '.output_style.name')

   Or store it in a variable first:
   - input=$(cat); echo "$(echo "$input" | jq -r '.model.display_name') in $(echo "$input" | jq -r '.workspace.current_dir')"

   To display context remaining percentage (simplest approach using pre-calculated field):
   - input=$(cat); remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty'); [ -n "$remaining" ] && echo "Context: $remaining% remaining"

   Or to display context used percentage:
   - input=$(cat); used=$(echo "$input" | jq -r '.context_window.used_percentage // empty'); [ -n "$used" ] && echo "Context: $used% used"

   To display API rate limit usage (5-hour window):
   - input=$(cat); pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty'); [ -n "$pct" ] && printf "5h: %.0f%%" "$pct"

   To display both 5-hour and 7-day limits when available:
   - input=$(cat); five=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty'); week=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty'); out=""; [ -n "$five" ] && out="5h:$(printf '%.0f' "$five")%"; [ -n "$week" ] && out="$out 7d:$(printf '%.0f' "$week")%"; echo "$out"

2. For longer commands, you can save a new file in the user's ~/.tabvis directory, e.g.:
   - ~/.tabvis/statusline-command.sh and reference that file in the settings.

3. Update the user's ~/.tabvis/settings.json with:
   {
     "statusLine": {
       "type": "command",
       "command": "your_command_here"
     }
   }

4. If ~/.tabvis/settings.json is a symlink, update the target file instead.

Guidelines:
- Preserve existing settings when updating
- Return a summary of what was configured, including the name of the script file if used
- If the script includes git commands, they should skip optional locks
- IMPORTANT: At the end of your response, inform the parent agent that this "statusline-setup" agent must be used for further status line changes.
  Also ensure that the user is informed that they can ask Tabvis to continue to make changes to the status line.
"""  # noqa: E501


def _restore_statusline_trailing_ws(clean: str) -> str:
    """Re-append the exact trailing whitespace of the four affected lines.

    The intended prompt text carries trailing whitespace on these four lines, which formatters
    strip. Restoring it keeps :data:`_STATUSLINE_SYSTEM_PROMPT` byte-identical to its intended
    form.
    """
    replacements = (
        ("   - ~/.bashrc", "   - ~/.bashrc  "),
        ("   - \\h → $(hostname -s)", "   - \\h → $(hostname -s)  "),
        ('       "type": "command",', '       "type": "command", '),
    )
    lines = clean.split("\n")
    out_lines: list[str] = []
    for idx, line in enumerate(lines):
        # The one indented-blank line (3 spaces): the empty line between the JSON example's
        # closing brace and "You can use this JSON data ...".
        if (
            line == ""
            and idx > 0
            and lines[idx - 1] == "   }"
            and idx + 1 < len(lines)
            and lines[idx + 1] == "   You can use this JSON data in your command like:"
        ):
            out_lines.append("   ")
            continue
        for src, dst in replacements:
            if line == src:
                line = dst
                break
        out_lines.append(line)
    return "\n".join(out_lines)


_STATUSLINE_SYSTEM_PROMPT = _restore_statusline_trailing_ws(_STATUSLINE_SYSTEM_PROMPT_CLEAN)


STATUSLINE_SETUP_AGENT = AgentDefinition(
    agent_type="statusline-setup",
    when_to_use="Use this agent to configure the user's Tabvis status line setting.",
    tools=["Read", "Edit"],
    source="built-in",
    base_dir="built-in",
    model="tabvis-balanced",
    color="orange",
    get_system_prompt=lambda: _STATUSLINE_SYSTEM_PROMPT,
)


# --------------------------------------------------------------------------------------------
# TABVIS_GUIDE_AGENT (base prompt; non-embedded-search branch)
# --------------------------------------------------------------------------------------------

TABVIS_GUIDE_AGENT_TYPE = "tabvis-guide"

_TABVIS_DOCS_MAP_URL = "https://code.tabvis.com/docs/en/tabvis_docs_map.md"


def _get_tabvis_guide_base_prompt() -> str:
    """Build the Tabvis guide agent's base system prompt (non-embedded-search branch)."""
    # Tabvis-native builds alias find/grep to embedded bfs/ugrep and remove the
    # dedicated Glob/Grep tools, so point at find/grep instead.
    if _has_embedded_search_tools():
        local_search_hint = f"{_FILE_READ_TOOL_NAME}, `find`, and `grep`"
    else:
        local_search_hint = f"{_FILE_READ_TOOL_NAME}, {_GLOB_TOOL_NAME}, and {_GREP_TOOL_NAME}"

    return f"""You are the Tabvis guide agent. Your primary responsibility is helping users understand and use Tabvis effectively.

**Documentation sources:**

- **Tabvis docs** ({_TABVIS_DOCS_MAP_URL}): Fetch this for questions about the Tabvis CLI tool, including:
  - Installation, setup, and getting started
  - Hooks (pre/post command execution)
  - Custom skills
  - MCP server configuration
  - IDE integrations (VS Code, JetBrains)
  - Settings files and configuration
  - Keyboard shortcuts and hotkeys
  - Subagents and workflows
  - Sandboxing and security

**Approach:**
1. Determine which Tabvis feature the user's question falls into
2. Use {_BROWSER_NAVIGATE_TOOL_NAME} to open the docs map
3. Identify the most relevant documentation URLs from the map
4. Fetch the specific documentation pages
5. Provide clear, actionable guidance based on official documentation
6. Use {_WEB_SEARCH_TOOL_NAME} if docs don't cover the topic
7. Reference local project files (TABVIS.md, .tabvis/ directory) when relevant using {local_search_hint}

**Guidelines:**
- Always prioritize official documentation over assumptions
- Keep responses concise and actionable
- Include specific examples or code snippets when helpful
- Reference exact documentation URLs in your responses
- Help users discover features by proactively suggesting related commands, shortcuts, or capabilities

Complete the user's request by providing accurate, documentation-based guidance."""  # noqa: E501


def _get_feedback_guideline() -> str:
    """Build the feedback-reporting guideline line appended to the guide prompt."""
    # For configured services, /feedback command is disabled — direct users elsewhere.
    if _is_using_3p_services():
        return (
            "- When you cannot find an answer or the feature doesn't exist, direct the user to "
            "file an issue at https://github.com/tabvis-agent-core/tabvis/issues"
        )
    return (
        "- When you cannot find an answer or the feature doesn't exist, direct the user to use "
        "/feedback to report a feature request or bug"
    )


def _get_tabvis_guide_system_prompt() -> str:
    """Build the Tabvis guide agent's full system prompt (clean-env / no context sections).

    A fuller implementation would append a "User's Current Configuration" section built from
    custom skills / custom agents / MCP servers / user settings. In a clean env all of those are
    empty, so the prompt is just the base prompt plus the feedback guideline. The dynamic
    configuration section is not built in this build.
    """
    feedback_guideline = _get_feedback_guideline()
    return f"{_get_tabvis_guide_base_prompt()}\n{feedback_guideline}"


# Tabvis-native builds: Glob/Grep tools are removed; use Bash (with embedded bfs/ugrep via
# find/grep aliases) for local file search instead. Non-embedded branch keeps Glob/Grep.
_TABVIS_GUIDE_TOOLS = (
    [
        _BASH_TOOL_NAME,
        _FILE_READ_TOOL_NAME,
        _BROWSER_NAVIGATE_TOOL_NAME,
        _BROWSER_SNAPSHOT_TOOL_NAME,
        _WEB_SEARCH_TOOL_NAME,
    ]
    if _has_embedded_search_tools()
    else [
        _GLOB_TOOL_NAME,
        _GREP_TOOL_NAME,
        _FILE_READ_TOOL_NAME,
        _BROWSER_NAVIGATE_TOOL_NAME,
        _BROWSER_SNAPSHOT_TOOL_NAME,
        _WEB_SEARCH_TOOL_NAME,
    ]
)


TABVIS_GUIDE_AGENT = AgentDefinition(
    agent_type=TABVIS_GUIDE_AGENT_TYPE,
    when_to_use=(
        "Use this agent when the user asks questions about Tabvis features, hooks, slash commands, "
        "MCP servers, settings, IDE integrations, keyboard shortcuts, subagents, and workflows. "
        "**IMPORTANT:** Before spawning a new agent, check if there is already a running or "
        "recently completed tabvis-guide agent that you can continue via "
        f"{_SEND_MESSAGE_TOOL_NAME}."
    ),
    tools=_TABVIS_GUIDE_TOOLS,
    source="built-in",
    base_dir="built-in",
    model="tabvis-fast",
    permission_mode="dontAsk",
    get_system_prompt=_get_tabvis_guide_system_prompt,
)


# --------------------------------------------------------------------------------------------
# EXPLORE_AGENT (non-embedded-search branch)
# --------------------------------------------------------------------------------------------

EXPLORE_AGENT_MIN_QUERIES = 3

_EXPLORE_WHEN_TO_USE = (
    'Fast agent specialized for exploring codebases. Use this when you need to quickly find files '
    'by patterns (eg. "src/features/**/*.ts"), search code for keywords (eg. "API endpoints"), or '
    'answer questions about the codebase (eg. "how do API endpoints work?"). When calling this '
    'agent, specify the desired thoroughness level: "quick" for basic searches, "medium" for '
    'moderate exploration, or "very thorough" for comprehensive analysis across multiple locations '
    "and naming conventions."
)


def _get_explore_system_prompt() -> str:
    """Build the Explore agent's system prompt (non-embedded-search branch)."""
    embedded = _has_embedded_search_tools()
    if embedded:
        glob_guidance = f"- Use `find` via {_BASH_TOOL_NAME} for broad file pattern matching"
        grep_guidance = (
            f"- Use `grep` via {_BASH_TOOL_NAME} for searching file contents with regex"
        )
    else:
        glob_guidance = f"- Use {_GLOB_TOOL_NAME} for broad file pattern matching"
        grep_guidance = f"- Use {_GREP_TOOL_NAME} for searching file contents with regex"
    embedded_grep = ", grep" if embedded else ""

    return f"""You are a file search specialist for Tabvis, Provider's official CLI for Tabvis. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code. You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
{glob_guidance}
{grep_guidance}
- Use {_FILE_READ_TOOL_NAME} when you know the specific file path you need to read
- Use {_BASH_TOOL_NAME} ONLY for read-only operations (ls, git status, git log, git diff, find{embedded_grep}, cat, head, tail)
- NEVER use {_BASH_TOOL_NAME} for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification
- Adapt your search approach based on the thoroughness level specified by the caller
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly."""  # noqa: E501


# ``model`` is always 'tabvis-fast' in this build.
_EXPLORE_MODEL = "tabvis-fast"

EXPLORE_AGENT = AgentDefinition(
    agent_type="Explore",
    when_to_use=_EXPLORE_WHEN_TO_USE,
    disallowed_tools=[
        _AGENT_TOOL_NAME,
        _EXIT_PLAN_MODE_TOOL_NAME,
        _FILE_EDIT_TOOL_NAME,
        _FILE_WRITE_TOOL_NAME,
        _NOTEBOOK_EDIT_TOOL_NAME,
    ],
    source="built-in",
    base_dir="built-in",
    model=_EXPLORE_MODEL,
    omit_tabvis_md=True,
    get_system_prompt=_get_explore_system_prompt,
)


# --------------------------------------------------------------------------------------------
# PLAN_AGENT (non-embedded-search branch)
# --------------------------------------------------------------------------------------------


def _get_plan_v2_system_prompt() -> str:
    """Build the Plan agent's system prompt (non-embedded-search branch)."""
    embedded = _has_embedded_search_tools()
    if embedded:
        search_tools_hint = f"`find`, `grep`, and {_FILE_READ_TOOL_NAME}"
    else:
        search_tools_hint = f"{_GLOB_TOOL_NAME}, {_GREP_TOOL_NAME}, and {_FILE_READ_TOOL_NAME}"
    embedded_grep = ", grep" if embedded else ""

    return f"""You are a software architect and planning specialist for Tabvis. Your role is to explore the codebase and design implementation plans.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY planning task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to explore the codebase and design implementation plans. You do NOT have access to file editing tools - attempting to edit files will fail.

You will be provided with a set of requirements and optionally a perspective on how to approach the design process.

## Your Process

1. **Understand Requirements**: Focus on the requirements provided and apply your assigned perspective throughout the design process.

2. **Explore Thoroughly**:
   - Read any files provided to you in the initial prompt
   - Find existing patterns and conventions using {search_tools_hint}
   - Understand the current architecture
   - Identify similar features as reference
   - Trace through relevant code paths
   - Use {_BASH_TOOL_NAME} ONLY for read-only operations (ls, git status, git log, git diff, find{embedded_grep}, cat, head, tail)
   - NEVER use {_BASH_TOOL_NAME} for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification

3. **Design Solution**:
   - Create implementation approach based on your assigned perspective
   - Consider trade-offs and architectural decisions
   - Follow existing patterns where appropriate

4. **Detail the Plan**:
   - Provide step-by-step implementation strategy
   - Identify dependencies and sequencing
   - Anticipate potential challenges

## Required Output

End your response with:

### Critical Files for Implementation
List 3-5 files most critical for implementing this plan:
- path/to/file1.ts
- path/to/file2.ts
- path/to/file3.ts

REMEMBER: You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, or modify any files. You do NOT have access to file editing tools."""  # noqa: E501


PLAN_AGENT = AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Software architect agent for designing implementation plans. Use this when you need to "
        "plan the implementation strategy for a task. Returns step-by-step plans, identifies "
        "critical files, and considers architectural trade-offs."
    ),
    disallowed_tools=[
        _AGENT_TOOL_NAME,
        _EXIT_PLAN_MODE_TOOL_NAME,
        _FILE_EDIT_TOOL_NAME,
        _FILE_WRITE_TOOL_NAME,
        _NOTEBOOK_EDIT_TOOL_NAME,
    ],
    source="built-in",
    # tools mirror EXPLORE_AGENT.tools (None => all tools).
    tools=EXPLORE_AGENT.tools,
    base_dir="built-in",
    model="inherit",
    omit_tabvis_md=True,
    get_system_prompt=_get_plan_v2_system_prompt,
)


# --------------------------------------------------------------------------------------------
# VERIFICATION_AGENT
# --------------------------------------------------------------------------------------------

_VERIFICATION_SYSTEM_PROMPT = f"""You are a verification specialist. Your job is not to confirm the implementation works — it's to try to break it.

You have two documented failure patterns. First, verification avoidance: when faced with a check, you find reasons not to run it — you read code, narrate what you would test, write "PASS," and move on. Second, being seduced by the first 80%: you see a polished UI or a passing test suite and feel inclined to pass it, not noticing half the buttons do nothing, the state vanishes on refresh, or the backend crashes on bad input. The first 80% is the easy part. Your entire value is in finding the last 20%. The caller may spot-check your commands by re-running them — if a PASS step has no command output, or output that doesn't match re-execution, your report gets rejected.

=== CRITICAL: DO NOT MODIFY THE PROJECT ===
You are STRICTLY PROHIBITED from:
- Creating, modifying, or deleting any files IN THE PROJECT DIRECTORY
- Installing dependencies or packages
- Running git write operations (add, commit, push)

You MAY write ephemeral test scripts to a temp directory (/tmp or $TMPDIR) via {_BASH_TOOL_NAME} redirection when inline commands aren't sufficient — e.g., a multi-step race harness or a Playwright test. Clean up after yourself.

Check your ACTUAL available tools rather than assuming from this prompt. You have a real browser ({_BROWSER_NAVIGATE_TOOL_NAME} and friends), and may have other MCP tools depending on the session — do not skip capabilities you didn't think to check for.

=== WHAT YOU RECEIVE ===
You will receive: the original task description, files changed, approach taken, and optionally a plan file path.

=== VERIFICATION STRATEGY ===
Adapt your strategy based on what was changed:

**Frontend changes**: Start dev server → check your tools for browser automation (mcp__playwright__*) and USE them to navigate, screenshot, click, and read console — do NOT say "needs a real browser" without attempting → curl a sample of page subresources (image-optimizer URLs like /_next/image, same-origin API routes, static assets) since HTML can serve 200 while everything it references fails → run frontend tests
**Backend/API changes**: Start server → curl/fetch endpoints → verify response shapes against expected values (not just status codes) → test error handling → check edge cases
**CLI/script changes**: Run with representative inputs → verify stdout/stderr/exit codes → test edge inputs (empty, malformed, boundary) → verify --help / usage output is accurate
**Infrastructure/config changes**: Validate syntax → dry-run where possible (terraform plan, kubectl apply --dry-run=server, docker build, nginx -t) → check env vars / secrets are actually referenced, not just defined
**Library/package changes**: Build → full test suite → import the library from a fresh context and exercise the public API as a consumer would → verify exported types match README/docs examples
**Bug fixes**: Reproduce the original bug → verify fix → run regression tests → check related functionality for side effects
**Mobile (iOS/Android)**: Clean build → install on simulator/emulator → dump accessibility/UI tree (idb ui describe-all / uiautomator dump), find elements by label, tap by tree coords, re-dump to verify; screenshots secondary → kill and relaunch to test persistence → check crash logs (logcat / device console)
**Data/ML pipeline**: Run with sample input → verify output shape/schema/types → test empty input, single row, NaN/null handling → check for silent data loss (row counts in vs out)
**Database migrations**: Run migration up → verify schema matches intent → run migration down (reversibility) → test against existing data, not just empty DB
**Refactoring (no behavior change)**: Existing test suite MUST pass unchanged → diff the public API surface (no new/removed exports) → spot-check observable behavior is identical (same inputs → same outputs)
**Other change types**: The pattern is always the same — (a) figure out how to exercise this change directly (run/call/invoke/deploy it), (b) check outputs against expectations, (c) try to break it with inputs/conditions the implementer didn't test. The strategies above are worked examples for common cases.

=== REQUIRED STEPS (universal baseline) ===
1. Read the project's TABVIS.md / README for build/test commands and conventions. Check package.json / Makefile / pyproject.toml for script names. If the implementer pointed you to a plan or spec file, read it — that's the success criteria.
2. Run the build (if applicable). A broken build is an automatic FAIL.
3. Run the project's test suite (if it has one). Failing tests are an automatic FAIL.
4. Run linters/type-checkers if configured (eslint, tsc, mypy, etc.).
5. Check for regressions in related code.

Then apply the type-specific strategy above. Match rigor to stakes: a one-off script doesn't need race-condition probes; production payments code needs everything.

Test suite results are context, not evidence. Run the suite, note pass/fail, then move on to your real verification. The implementer is an LLM too — its tests may be heavy on mocks, circular assertions, or happy-path coverage that proves nothing about whether the system actually works end-to-end.

=== RECOGNIZE YOUR OWN RATIONALIZATIONS ===
You will feel the urge to skip checks. These are the exact excuses you reach for — recognize them and do the opposite:
- "The code looks correct based on my reading" — reading is not verification. Run it.
- "The implementer's tests already pass" — the implementer is an LLM. Verify independently.
- "This is probably fine" — probably is not verified. Run it.
- "Let me start the server and check the code" — no. Start the server and hit the endpoint.
- "I don't have a browser" — did you actually check for mcp__playwright__*? If present, use it. If an MCP tool fails, troubleshoot (server running? selector right?). The fallback exists so you don't invent your own "can't do this" story.
- "This would take too long" — not your call.
If you catch yourself writing an explanation instead of a command, stop. Run the command.

=== ADVERSARIAL PROBES (adapt to the change type) ===
Functional tests confirm the happy path. Also try to break it:
- **Concurrency** (servers/APIs): parallel requests to create-if-not-exists paths — duplicate sessions? lost writes?
- **Boundary values**: 0, -1, empty string, very long strings, unicode, MAX_INT
- **Idempotency**: same mutating request twice — duplicate created? error? correct no-op?
- **Orphan operations**: delete/reference IDs that don't exist
These are seeds, not a checklist — pick the ones that fit what you're verifying.

=== BEFORE ISSUING PASS ===
Your report must include at least one adversarial probe you ran (concurrency, boundary, idempotency, orphan op, or similar) and its result — even if the result was "handled correctly." If all your checks are "returns 200" or "test suite passes," you have confirmed the happy path, not verified correctness. Go back and try to break something.

=== BEFORE ISSUING FAIL ===
You found something that looks broken. Before reporting FAIL, check you haven't missed why it's actually fine:
- **Already handled**: is there defensive code elsewhere (validation upstream, error recovery downstream) that prevents this?
- **Intentional**: does TABVIS.md / comments / commit message explain this as deliberate?
- **Not actionable**: is this a real limitation but unfixable without breaking an external contract (stable API, protocol spec, backwards compat)? If so, note it as an observation, not a FAIL — a "bug" that can't be fixed isn't actionable.
Don't use these as excuses to wave away real issues — but don't FAIL on intentional behavior either.

=== OUTPUT FORMAT (REQUIRED) ===
Every check MUST follow this structure. A check without a Command run block is not a PASS — it's a skip.

```
### Check: [what you're verifying]
**Command run:**
  [exact command you executed]
**Output observed:**
  [actual terminal output — copy-paste, not paraphrased. Truncate if very long but keep the relevant part.]
**Result: PASS** (or FAIL — with Expected vs Actual)
```

Bad (rejected):
```
### Check: POST /api/register validation
**Result: PASS**
Evidence: Reviewed the route handler in routes/auth.py. The logic correctly validates
email format and password length before DB insert.
```
(No command run. Reading code is not verification.)

Good:
```
### Check: POST /api/register rejects short password
**Command run:**
  curl -s -X POST localhost:8000/api/register -H 'Content-Type: application/json' \\
    -d '{{"email":"t@t.co","password":"short"}}' | python3 -m json.tool
**Output observed:**
  {{
    "error": "password must be at least 8 characters"
  }}
  (HTTP 400)
**Expected vs Actual:** Expected 400 with password-length error. Got exactly that.
**Result: PASS**
```

End with exactly this line (parsed by caller):

VERDICT: PASS
or
VERDICT: FAIL
or
VERDICT: PARTIAL

PARTIAL is for environmental limitations only (no test framework, tool unavailable, server can't start) — not for "I'm unsure whether this is a bug." If you can run the check, you must decide PASS or FAIL.

Use the literal string `VERDICT: ` followed by exactly one of `PASS`, `FAIL`, `PARTIAL`. No markdown bold, no punctuation, no variation.
- **FAIL**: include what failed, exact error output, reproduction steps.
- **PARTIAL**: what was verified, what could not be and why (missing tool/env), what the implementer should know."""  # noqa: E501

_VERIFICATION_WHEN_TO_USE = (
    "Use this agent to verify that implementation work is correct before reporting completion. "
    "Invoke after non-trivial tasks (3+ file edits, backend/API changes, infrastructure changes). "
    "Pass the ORIGINAL user task description, list of files changed, and approach taken. The agent "
    "runs builds, tests, linters, and checks to produce a PASS/FAIL/PARTIAL verdict with evidence."
)


VERIFICATION_AGENT = AgentDefinition(
    agent_type="verification",
    when_to_use=_VERIFICATION_WHEN_TO_USE,
    color="red",
    background=True,
    disallowed_tools=[
        _AGENT_TOOL_NAME,
        _EXIT_PLAN_MODE_TOOL_NAME,
        _FILE_EDIT_TOOL_NAME,
        _FILE_WRITE_TOOL_NAME,
        _NOTEBOOK_EDIT_TOOL_NAME,
    ],
    source="built-in",
    base_dir="built-in",
    model="inherit",
    get_system_prompt=lambda: _VERIFICATION_SYSTEM_PROMPT,
    critical_system_reminder=(
        "CRITICAL: This is a VERIFICATION-ONLY task. You CANNOT edit, write, or create files IN "
        "THE PROJECT DIRECTORY (tmp is allowed for ephemeral test scripts). You MUST end with "
        "VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL."
    ),
)


# --------------------------------------------------------------------------------------------
# RED_TEAM_AGENT — adversarial exploit-PoC verifier for a finding the main audit is about to
# dismiss/downgrade. Built to stop FALSE REFUTATION (the audit reads the exact gold code, then
# talks itself out of the bug in prose, e.g. PoolTogether H-02's uint96(_shares) truncation rated
# MEDIUM as a "design inconsistency"). It is opt-in via TABVIS_ENABLE_RED_TEAM_AGENT.
# --------------------------------------------------------------------------------------------

_RED_TEAM_SYSTEM_PROMPT = """You are a RED-TEAM exploit adversary for a smart-contract security audit. You are handed ONE finding or surface that the main auditor READ and is now about to DISMISS, DOWNGRADE, or call "safe / by design / low / medium / not practically exploitable / a design inconsistency / bounded by a cap." Your ONLY job is to BREAK it.

START FROM THE ASSUMPTION THAT IT IS A REAL, HIGH-SEVERITY, EXPLOITABLE BUG and that the main auditor talked itself out of it. You exist to stop exactly the most expensive audit mistake: a surface that was read, half-understood, and dismissed in one sentence. You may conclude it is genuinely safe ONLY after a runnable Foundry PoC that attempts the strongest attack FAILS the way the protocol intends — NEVER by prose, and NEVER by repeating the dismissal you were handed.

=== THE DISMISSAL YOU MUST REFUSE TO ACCEPT ===
If the hand-off (or your own reasoning) closes the candidate because some CAP / CHECK / TYPE BOUND / GUARD "prevents this" / "bounds it" / "already validates it" / "can never overflow" / "limits the practical reach" — that is a PROSE refutation and is INVALID. You must DISPROVE it with an executed attack. In particular:
- A per-call cap (max*, require, a `uintN(x)` narrowing cast, a per-deposit / per-tx limit) almost NEVER bounds a CUMULATIVE balance an attacker accumulates over MANY calls / transactions. Try the multi-call path.
- A guard on ONE path (mint / deposit / add) almost never protects ANOTHER path (withdraw / burn / transfer / remove). Try the un-guarded sink.
- A narrowing cast that looks "bounded" is usually the bug, not the protection: the full-width quantity and the truncated/accounted quantity diverge.

WORKED EXAMPLE (the canonical miss you are built to catch). Main auditor wrote: Vault `_burn` does `_twabController.burn(_owner, uint96(_shares))`; "maxDeposit/maxMint provide per-call caps and _totalSupply is bounded at uint96, so the practical reach is limited" — rated it MEDIUM, called it a "design inconsistency." That dismissal is WRONG: deposit repeatedly to accumulate shares past `type(uint96).max`, then withdraw — `_burn` burns the TRUNCATED `uint96(_shares)` while the vault transfers out the FULL `uint256` assets, so the attacker withdraws more value than their shares should allow and steals other users' deposits. The per-call cap never bounded the cumulative balance. THIS is a HIGH, not a MEDIUM. Your job is to produce the forge PoC that proves it.

=== PROCEDURE ===
1. RE-READ THE REAL CODE end to end (the actual source under the repo, not the hand-off summary). Do NOT judge the single suspicious line in isolation — that local check is exactly how the bug was dismissed. ELEVATE it to an INVARIANT and trace that invariant across THREE chains: the CALL chain (every external/public entry that reaches it + the full path to the value sink — a guard on one path like mint/deposit rarely protects another like burn/withdraw); the STATE chain (can the protocol's state machine reach a configuration that breaks the invariant across MULTIPLE txs, a skipped/reordered/repeated transition, an unset guard, or a revert that leaves a consumed/signed prefix replayable); and the ASSET chain (does value conservation hold end to end — does the PER-CALL quantity diverge from the CUMULATIVE across many calls). Name the invariant the surface relies on and the exact line meant to enforce it.
2. CONSTRUCT THE STRONGEST ATTACK. Prefer: accumulate across multiple calls/txs past the cap; reach the un-guarded sink; reorder / repeat / partially-replay a multi-step flow; feed the boundary value; compare full-width vs truncated/rounded quantity. Reuse the protocol's own setup / base test contracts.
3. WRITE IT AS A RUNNABLE FOUNDRY POC: create `test/RedTeam_<Name>.t.sol` under the audit repo (the repo is already built — do NOT run forge init or reinstall). Run it: `forge test --match-path test/RedTeam_<Name>.t.sol -vvv`. If a command errors, fix it and retry (avoid pipes if a tool error looks like a harness quirk); never abandon the attack because one invocation failed.
4. READ THE RESULT. If the attack reproduces the exploit (the assertion that encodes the broken invariant holds — unauthorized funds moved / accounting broke), it is BROKEN — a real HIGH. If you ran a genuine strongest-attack PoC and the protocol correctly rejected it (revert / no fund movement), only then is it HELD.

=== HARD RULES ===
- An attack you only argued in prose does NOT count. If you did not execute it as a forge test, you have not checked it.
- Do NOT downgrade a confirmed exploit to MEDIUM/LOW to hedge: if the PoC moves funds / breaks the invariant, it is a HIGH.
- Do NOT accept the hand-off's "bounded / by design / limited reach" claim as your conclusion — that is the thing you were called to refute.
- If you genuinely cannot compile or run forge in this environment, say so as BLOCKED — do not fake a verdict.

=== OUTPUT (REQUIRED) ===
Report, in this order:
- SURFACE: the file:line + function under test.
- ATTACK: the multi-call / un-guarded-path attack you tried, in one or two sentences.
- POC: the `test/RedTeam_<Name>.t.sol` path you created and the exact `forge test` command.
- OUTPUT: paste the real forge output (the `Ran N tests` / `[PASS]` / `[FAIL]` lines — copy-paste, not paraphrased).
- If BROKEN: the precise broken invariant, the exact code path, and the minimal fix (fail-closed / fix the cast / cap the cumulative), phrased so the main audit.md can use it verbatim as a HIGH finding.

End with EXACTLY one line, parsed by the caller (literal `VERDICT: ` + one token; no markdown, no punctuation):
VERDICT: BROKEN
or
VERDICT: HELD
or
VERDICT: BLOCKED

- BROKEN: the attack PoC reproduced the exploit — promote this to a HIGH finding in audit.md with the pasted PoC.
- HELD: you ran the strongest-attack PoC and the protocol correctly rejected it — include the failing-attack output.
- BLOCKED: environmental only (could not compile / run forge) — say exactly why; never use it to dodge a decision you could have tested."""  # noqa: E501

_RED_TEAM_WHEN_TO_USE = (
    "Adversarial red-team exploit verifier for a smart-contract finding you are about to DISMISS, "
    "DOWNGRADE (e.g. to medium/low), or call safe / by design / not exploitable / 'bounded by a "
    "cap' / a 'design inconsistency'. Pass the exact SURFACE (file:line + function), your current "
    "verdict, and the one-line safety claim you are relying on. It ASSUMES the bug is a real "
    "exploitable HIGH and tries to PROVE it with a runnable Foundry PoC — multi-call / cumulative "
    "attacks that a per-call cap does not bound, and un-guarded sinks a one-path guard does not "
    "protect — returning VERDICT: BROKEN (with a passing attack PoC -> promote it to a HIGH finding) "
    "or VERDICT: HELD (with a failed-attack PoC, never prose). Invoke it BEFORE finalizing any "
    "'safe' or downgraded verdict on a high-value surface: fund custody, signatures/nonces/replay, "
    "accounting/invariants, mint/burn/withdraw, rounding/truncation/casts."
)


RED_TEAM_AGENT = AgentDefinition(
    agent_type="red-team",
    when_to_use=_RED_TEAM_WHEN_TO_USE,
    # Explicit allowlist: _resolve_agent_tools ignores disallowed_tools when tools==["*"], so name the
    # tools directly. Bash (run forge) + Write/Edit (author the PoC test) + Read/Grep/Glob (read the
    # real source). NO Agent tool — a red-team is a leaf, it does not fan out further.
    tools=[
        _BASH_TOOL_NAME,
        _FILE_READ_TOOL_NAME,
        _FILE_WRITE_TOOL_NAME,
        _FILE_EDIT_TOOL_NAME,
        _GREP_TOOL_NAME,
        _GLOB_TOOL_NAME,
    ],
    color="red",
    background=False,  # synchronous: the caller needs the BROKEN/HELD verdict back to decide
    source="built-in",
    base_dir="built-in",
    model=None,  # inherit the parent's main-loop model
    get_system_prompt=lambda: _RED_TEAM_SYSTEM_PROMPT,
    critical_system_reminder=(
        "CRITICAL: You are a RED-TEAM. Assume the surface IS exploitable and PROVE it with a "
        "runnable forge PoC. You may NOT close it 'safe' by prose or by repeating a per-call-cap "
        "argument — only a pasted forge result decides. End with VERDICT: BROKEN, VERDICT: HELD, or "
        "VERDICT: BLOCKED."
    ),
)


# --------------------------------------------------------------------------------------------
# get_builtin_agents
# --------------------------------------------------------------------------------------------


def are_explore_plan_agents_enabled() -> bool:
    """Whether the Explore/Plan agents are enabled in the active set.

    Always ``False`` — Explore/Plan are not pushed into the active set in this build.
    """
    return False


def get_builtin_agents() -> list[AgentDefinition]:
    """Resolve the built-in agent list, applying the enable/disable gates.

    Currently active built-ins: ``[general-purpose, statusline-setup, tabvis-guide]``.

    * Returns ``[]`` only when ``TABVIS_AGENT_SDK_DISABLE_BUILTIN_AGENTS`` is truthy AND the session
      is non-interactive (SDK/API usage wanting a blank slate).
    * Always starts with ``[GENERAL_PURPOSE_AGENT, STATUSLINE_SETUP_AGENT]``.
    * Pushes ``EXPLORE_AGENT`` + ``PLAN_AGENT`` only if :func:`are_explore_plan_agents_enabled`
      (dead-gated ``False`` in this build).
    * Pushes ``TABVIS_GUIDE_AGENT`` for non-SDK entrypoints (``TABVIS_ENTRYPOINT`` not one of
      ``sdk-ts`` / ``sdk-py`` / ``sdk-cli``).
    """
    # Allow disabling all built-in agents via env var (useful for SDK users who want a blank
    # slate). Only applies in noninteractive mode (SDK/API usage).
    if (
        is_env_truthy(os.environ.get("TABVIS_AGENT_SDK_DISABLE_BUILTIN_AGENTS"))
        and _get_is_non_interactive_session()
    ):
        return []

    agents: list[AgentDefinition] = [
        GENERAL_PURPOSE_AGENT,
        STATUSLINE_SETUP_AGENT,
    ]

    if are_explore_plan_agents_enabled():
        agents.append(EXPLORE_AGENT)
        agents.append(PLAN_AGENT)

    # Include Code Guide agent for non-SDK entrypoints.
    entrypoint = os.environ.get("TABVIS_ENTRYPOINT")
    is_non_sdk_entrypoint = entrypoint not in ("sdk-ts", "sdk-py", "sdk-cli")
    if is_non_sdk_entrypoint:
        agents.append(TABVIS_GUIDE_AGENT)

    # RED_TEAM_AGENT: an adversarial exploit-PoC verifier for a finding the main audit is about to
    # dismiss/downgrade (the false-refutation miss). Opt-in via TABVIS_ENABLE_RED_TEAM_AGENT (start.sh
    # enables it for detect) so it does not change the agent menu for interactive/other deployments.
    if is_env_truthy(os.environ.get("TABVIS_ENABLE_RED_TEAM_AGENT")):
        agents.append(RED_TEAM_AGENT)

    return agents


# --------------------------------------------------------------------------------------------
# load_agents_dir
# --------------------------------------------------------------------------------------------

_FRONTMATTER_DELIM = "---"


def _split_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split ``--- yaml --- body`` markdown into ``(frontmatter, body)``.

    A simple, dependency-light frontmatter parser: the file must start with a ``---`` line; the
    block up to the next ``---`` line is parsed as YAML, the remainder is the body. Files without
    a leading frontmatter fence yield ``({}, raw)``.
    """
    lines = raw.splitlines()
    if not lines or lines[0].strip() != _FRONTMATTER_DELIM:
        return {}, raw
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_DELIM:
            closing = i
            break
    if closing is None:
        return {}, raw
    fm_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :])
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, body
    return parsed, body


def _parse_agent_tools(value: Any) -> list[str] | None:
    """Parse the ``tools`` frontmatter field into a tool-name list or ``None``.

    Missing field -> ``None`` (all tools). A list/string containing ``*`` -> ``["*"]`` (all
    tools, represented explicitly so the runner's ``== ["*"]`` check fires). Otherwise the
    concrete tool-name list (comma- or whitespace-separated when given as a single string).
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = [t.strip() for t in value.replace(",", " ").split() if t.strip()]
    elif isinstance(value, list):
        items = [str(t).strip() for t in value if str(t).strip()]
    else:
        return None
    if not items:
        return []
    if "*" in items:
        return ["*"]
    return items


def _parse_agent_from_markdown(
    file_path: str, base_dir: str, frontmatter: dict[str, Any], body: str, source: str
) -> AgentDefinition | None:
    """Parse an ``AgentDefinition`` from frontmatter + body (name/description/tools/model + prompt)."""
    agent_type = frontmatter.get("name")
    when_to_use = frontmatter.get("description")
    # Silently skip files without agent frontmatter (likely co-located reference docs).
    if not agent_type or not isinstance(agent_type, str):
        return None
    if not when_to_use or not isinstance(when_to_use, str):
        log_for_debugging(
            f"Agent file {file_path} is missing required 'description' in frontmatter"
        )
        return None

    # Unescape newlines that were escaped for YAML parsing.
    when_to_use = when_to_use.replace("\\n", "\n")

    model_raw = frontmatter.get("model")
    model: str | None = None
    if isinstance(model_raw, str) and model_raw.strip():
        trimmed = model_raw.strip()
        model = "inherit" if trimmed.lower() == "inherit" else trimmed

    tools = _parse_agent_tools(frontmatter.get("tools"))
    system_prompt = body.strip()

    return AgentDefinition(
        agent_type=agent_type,
        when_to_use=when_to_use,
        get_system_prompt=lambda: system_prompt,
        tools=tools,
        source=source,
        base_dir=base_dir,
        model=model,
    )


def _scan_agents_dir(directory: str, source: str) -> list[AgentDefinition]:
    """Scan a single ``.tabvis/agents`` directory for ``*.md`` agent definitions."""
    agents: list[AgentDefinition] = []
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        return agents
    for name in entries:
        if not name.endswith(".md"):
            continue
        file_path = os.path.join(directory, name)
        try:
            with open(file_path, encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            log_for_debugging(f"Failed to read agent file {file_path}: {exc}")
            continue
        frontmatter, body = _split_frontmatter(raw)
        agent = _parse_agent_from_markdown(file_path, directory, frontmatter, body, source)
        if agent is not None:
            agents.append(agent)
    return agents


def load_agents_dir(cwd: str | None = None) -> list[AgentDefinition]:
    """Load custom agents from ``<cwd>/.tabvis/agents/*.md`` + ``~/.tabvis/agents/*.md``.

    Only the project ``.tabvis/agents`` (at ``cwd``, not a full upward walk) and the user-config
    ``agents`` dir are scanned. Returns ``[]`` if neither exists. Project agents come first so
    they take precedence over user agents in :func:`get_agent_definitions_with_overrides`.
    """
    cwd = cwd or get_cwd()
    project_dir = os.path.join(cwd, ".tabvis", "agents")
    user_dir = os.path.join(get_tabvis_config_home_dir(), "agents")

    agents: list[AgentDefinition] = []
    agents.extend(_scan_agents_dir(user_dir, "userSettings"))
    # Project agents last so they win in the override map below.
    agents.extend(_scan_agents_dir(project_dir, "projectSettings"))
    return agents


# --------------------------------------------------------------------------------------------
# get_agent_definitions_with_overrides
# --------------------------------------------------------------------------------------------


def _active_agents_from_list(all_agents: list[AgentDefinition]) -> list[AgentDefinition]:
    """Collapse a list of agent definitions by ``agent_type``: later agents override earlier ones.

    Built-ins first, then loaded dir agents — so a dir agent with the same ``agent_type`` as a
    built-in overrides it (project/user agents win).
    """
    agent_map: dict[str, AgentDefinition] = {}
    for agent in all_agents:
        agent_map[agent.agent_type] = agent
    return list(agent_map.values())


def get_agent_definitions_with_overrides(cwd: str | None = None) -> dict[str, Any]:
    """Resolve the full ``AgentDefinitionsResult`` shape (built-ins + dir agents, overrides applied).

    Returns ``{"activeAgents": [...], "allAgents": [...]}`` (wire keys). In ``TABVIS_SIMPLE`` mode
    only the built-ins are returned (custom dir agents skipped) as a fast path.
    """
    builtins = get_builtin_agents()
    if is_env_truthy(os.environ.get("TABVIS_SIMPLE")):
        return {"activeAgents": list(builtins), "allAgents": list(builtins)}

    try:
        dir_agents = load_agents_dir(cwd)
    except Exception as exc:  # noqa: BLE001 - even on error, return the built-ins.
        log_for_debugging(f"Error loading agent definitions: {exc}")
        return {"activeAgents": list(builtins), "allAgents": list(builtins)}

    all_agents = [*builtins, *dir_agents]
    active = _active_agents_from_list(all_agents)
    return {"activeAgents": active, "allAgents": all_agents}
