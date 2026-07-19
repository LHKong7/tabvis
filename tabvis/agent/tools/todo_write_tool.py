"""TodoWrite tool — replace the session task checklist.

Replaces the agent's session task checklist in :data:`AppState.todos`, keyed by the calling
agent's ``agentId`` (falling back to the session id for the main thread). :class:`TodoWriteTool`
subclasses :class:`tabvis.tool.Tool` and is exported as the singleton :data:`todo_write_tool`. Its
input schema is a pydantic v2 ``BaseModel`` (:class:`TodoWriteInput`) with ``extra='forbid'``
wrapping a list of :class:`TodoItem` (``content``/``status``/``activeForm``).

Behavior notes:

* ``call`` reads the prior list via ``context.get_app_state()`` (``appState.todos[todoKey]``),
  computes ``allDone`` (every item ``completed``), and writes ``[]`` when all done — but still
  returns the *submitted* ``todos`` as ``newTodos`` in the output ``data``.
* The verification-nudge branch is unreachable (guarded by a literal ``False`` condition), so
  ``verificationNudgeNeeded`` is always ``False``. The nudge text and its supporting constant are
  kept in place but never emitted.
* ``is_enabled`` reports the inverse of :func:`is_todo_v2_enabled` — TodoWrite is the V1 path,
  disabled when the V2 Task tools are active. In a non-interactive (headless) session V2 is OFF,
  so TodoWrite is enabled (unless ``TABVIS_ENABLE_TASKS`` force-enables V2).
"""

from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tabvis.tool import Tool, ToolResult, ToolUseContext
from tabvis.types.can_use_tool import CanUseToolFn
from tabvis.types.message import AssistantMessage
from tabvis.types.permissions import PermissionDecision
from tabvis.utils.env_utils import is_env_truthy

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TODO_WRITE_TOOL_NAME = "TodoWrite"

# Used only by the (unreachable) verification-nudge branch below; kept local rather than
# importing a separate agent-tool module for one constant.
VERIFICATION_AGENT_TYPE = "verification"

# Kept local rather than importing a separate file-edit-tool module for one constant.
FILE_EDIT_TOOL_NAME = "Edit"

DESCRIPTION = (
    "Update the todo list for the current session. To be used proactively and often to track "
    "progress and pending tasks. Make sure that at least one task is in_progress at all times. "
    "Always provide both content (imperative) and activeForm (present continuous) for each task."
)

PROMPT = f"""Use this tool to create and manage a structured task list for your current coding session. This helps you track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

## When to Use This Tool
Use this tool proactively in these scenarios:

1. Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as todos
6. When you start working on a task - Mark it as in_progress BEFORE beginning work. Ideally you should only have one todo as in_progress at a time
7. After completing a task - Mark it as completed and add any new follow-up tasks discovered during implementation

## When NOT to Use This Tool

Skip using this tool when:
1. There is only a single, straightforward task
2. The task is trivial and tracking it provides no organizational benefit
3. The task can be completed in less than 3 trivial steps
4. The task is purely conversational or informational

NOTE that you should not use this tool if there is only one trivial task to do. In this case you are better off just doing the task directly.

## Examples of When to Use the Todo List

<example>
User: I want to add a dark mode toggle to the application settings. Make sure you run the tests and build when you're done!
Assistant: *Creates todo list with the following items:*
1. Creating dark mode toggle component in Settings page
2. Adding dark mode state management (context/store)
3. Implementing CSS-in-JS styles for dark theme
4. Updating existing components to support theme switching
5. Running tests and build process, addressing any failures or errors that occur
*Begins working on the first task*

<reasoning>
The assistant used the todo list because:
1. Adding dark mode is a multi-step feature requiring UI, state management, and styling changes
2. The user explicitly requested tests and build be run afterward
3. The assistant inferred that tests and build need to pass by adding "Ensure tests and build succeed" as the final task
</reasoning>
</example>

<example>
User: Help me rename the function getCwd to getCurrentWorkingDirectory across my project
Assistant: *Uses grep or search tools to locate all instances of getCwd in the codebase*
I've found 15 instances of 'getCwd' across 8 different files.
*Creates todo list with specific items for each file that needs updating*

<reasoning>
The assistant used the todo list because:
1. First, the assistant searched to understand the scope of the task
2. Upon finding multiple occurrences across different files, it determined this was a complex task with multiple steps
3. The todo list helps ensure every instance is tracked and updated systematically
4. This approach prevents missing any occurrences and maintains code consistency
</reasoning>
</example>


<example>
User: I need to implement these features for my e-commerce site: user registration, product catalog, shopping cart, and checkout flow.
Assistant: *Creates a todo list breaking down each feature into specific tasks based on the project architecture*
Let's start with user registration. This will involve creating database models, API endpoints, and frontend forms that integrate with your existing authentication system.

<reasoning>
The assistant used the todo list because:
1. The user provided multiple complex features to implement in a comma separated list
2. The todo list helps organize these large features into manageable tasks
3. This approach allows for tracking progress across the entire implementation
</reasoning>
</example>

<example>
User: Can you help optimize my React application? It's rendering slowly and has performance issues.
Assistant: *Reviews component structure, render patterns, state management, and data fetching*
After analyzing your codebase, I've identified several performance issues.
*Creates todo list with items like: 1) Implementing memoization for expensive calculations in ProductList, 2) Adding virtualization for long lists in Dashboard, 3) Optimizing image loading in Gallery component, 4) Fixing state update loops in ShoppingCart, 5) Reviewing bundle size and implementing code splitting*
Let's start by implementing memoization for the expensive calculations in your ProductList component.

<reasoning>
The assistant used the todo list because:
1. First, the assistant examined the codebase to identify specific performance issues
2. Based on this analysis, it identified multiple optimization opportunities
3. Performance optimization is a non-trivial task requiring multiple steps
4. The todo list helps methodically track improvements across different components
5. This systematic approach ensures all performance bottlenecks are addressed
</reasoning>
</example>

## Examples of When NOT to Use the Todo List

<example>
User: How do I print 'Hello World' in Python?
Assistant: In Python, you can print "Hello World" with this simple code:

python
print("Hello World")

This will output the text "Hello World" to the console when executed.

<reasoning>
The assistant did not use the todo list because this is a single, trivial task that can be completed in one step. There's no need to track multiple tasks or steps for such a straightforward request.
</reasoning>
</example>

<example>
User: What does the git status command do?
Assistant: The git status command shows the current state of your working directory and staging area. It displays which changes have been staged, which haven't, and which files aren't being tracked by Git. This is a fundamental command for understanding the current state of your repository before making commits.

<reasoning>
The assistant did not use the todo list because this is an informational request with no actual coding task to complete. The user is simply asking for an explanation, not for the assistant to perform multiple steps or tasks.
</reasoning>
</example>

<example>
User: Can you add a comment to the calculateTotal function to explain what it does?
Assistant: Sure, let me add a comment to the calculateTotal function to explain what it does.
* Uses the {FILE_EDIT_TOOL_NAME} tool to add a comment to the calculateTotal function *

<reasoning>
The assistant did not use the todo list because this is a single, straightforward task confined to one location in the code. Adding a comment doesn't require tracking multiple steps or systematic organization.
</reasoning>
</example>

<example>
User: Run npm install for me and tell me what happens.
Assistant: I'll run the npm install command for you.

*Executes: npm install*

The command completed successfully. Here's the output:
[Output of npm install command]

All dependencies have been installed according to your package.json file.

<reasoning>
The assistant did not use the todo list because this is a single command execution with immediate results. There are no multiple steps to track or organize, making the todo list unnecessary for this straightforward task.
</reasoning>
</example>

## Task States and Management

1. **Task States**: Use these states to track progress:
   - pending: Task not yet started
   - in_progress: Currently working on (limit to ONE task at a time)
   - completed: Task finished successfully

   **IMPORTANT**: Task descriptions must have two forms:
   - content: The imperative form describing what needs to be done (e.g., "Run tests", "Build the project")
   - activeForm: The present continuous form shown during execution (e.g., "Running tests", "Building the project")

2. **Task Management**:
   - Update task status in real-time as you work
   - Mark tasks complete IMMEDIATELY after finishing (don't batch completions)
   - Exactly ONE task must be in_progress at any time (not less, not more)
   - Complete current tasks before starting new ones
   - Remove tasks that are no longer relevant from the list entirely

3. **Task Completion Requirements**:
   - ONLY mark a task as completed when you have FULLY accomplished it
   - If you encounter errors, blockers, or cannot finish, keep the task as in_progress
   - When blocked, create a new task describing what needs to be resolved
   - Never mark a task as completed if:
     - Tests are failing
     - Implementation is partial
     - You encountered unresolved errors
     - You couldn't find necessary files or dependencies

4. **Task Breakdown**:
   - Create specific, actionable items
   - Break complex tasks into smaller, manageable steps
   - Use clear, descriptive task names
   - Always provide both forms:
     - content: "Fix authentication bug"
     - activeForm: "Fixing authentication bug"

When in doubt, use this tool. Being proactive with task management demonstrates attentiveness and ensures you complete all requirements successfully.
"""  # noqa: E501


# ---------------------------------------------------------------------------
# Session id fallback.
#
# The session id is only used as the AppState.todos key when the call has no agentId (the main
# thread), so any stable per-process value keeps old/new reads consistent within a process. A
# simple per-pid id is used here rather than a shared bootstrap-state singleton.
# ---------------------------------------------------------------------------

_FALLBACK_SESSION_ID = f"session-{os.getpid()}"


def get_session_id() -> str:
    return _FALLBACK_SESSION_ID


# ---------------------------------------------------------------------------
# is_todo_v2_enabled
#
# This build is always a non-interactive session, so TodoV2 is OFF and TodoWrite (V1) is enabled,
# unless TABVIS_ENABLE_TASKS force-enables V2.
# ---------------------------------------------------------------------------

_HEADLESS_IS_NON_INTERACTIVE = True


def is_todo_v2_enabled() -> bool:
    # Force-enable tasks (V2) in non-interactive mode via TABVIS_ENABLE_TASKS (SDK opt-in).
    if is_env_truthy(os.environ.get("TABVIS_ENABLE_TASKS")):
        return True
    return not _HEADLESS_IS_NON_INTERACTIVE


# ---------------------------------------------------------------------------
# Input schema
# ---------------------------------------------------------------------------

TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(BaseModel):
    """A single todo: content / status / activeForm.

    ``content`` and ``activeForm`` are non-empty strings. Extra keys are *allowed* on the item
    (only the top-level ``todos`` wrapper forbids extras), so this inner model does NOT forbid
    extras.
    """

    content: str = Field(min_length=1, description="Content cannot be empty")
    status: TodoStatus
    active_form: str = Field(
        min_length=1,
        alias="activeForm",
        description="Active form cannot be empty",
    )

    model_config = ConfigDict(populate_by_name=True)


class TodoWriteInput(BaseModel):
    """Validated input for :data:`todo_write_tool`."""

    model_config = ConfigDict(extra="forbid")

    todos: list[TodoItem] = Field(description="The updated todo list")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _todo_to_wire(item: TodoItem) -> dict[str, Any]:
    """Serialize a :class:`TodoItem` back to its wire dict (``activeForm`` alias)."""
    return item.model_dump(by_alias=True)


def _get_app_state_todos(app_state: Any) -> dict[str, Any]:
    """Read ``appState.todos`` (a dict keyed by todoKey), None-safe."""
    if app_state is None:
        return {}
    if isinstance(app_state, dict):
        todos = app_state.get("todos")
    else:
        todos = getattr(app_state, "todos", None)
    return todos if isinstance(todos, dict) else {}


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


class TodoWriteTool(Tool):
    """``TodoWrite`` — replace the session task checklist in AppState."""

    name = TODO_WRITE_TOOL_NAME
    search_hint = "manage the session task checklist"
    input_schema = TodoWriteInput
    max_result_size_chars = 100_000
    strict = True
    should_defer = True

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        return DESCRIPTION

    async def prompt(self, options: dict[str, Any]) -> str:
        return PROMPT

    def user_facing_name(self, input: Any | None = None) -> str:
        return ""

    def is_enabled(self) -> bool:
        return not is_todo_v2_enabled()

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionDecision:
        # No permission checks required for todo operations.
        return {"behavior": "allow", "updatedInput": input}

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[dict[str, Any]]:
        todos: list[TodoItem] = args.todos

        app_state = context.get_app_state() if context.get_app_state else None
        todo_key = context.agent_id if context.agent_id else get_session_id()

        existing = _get_app_state_todos(app_state)
        old_todos = existing.get(todo_key, [])

        all_done = len(todos) > 0 and all(t.status == "completed" for t in todos)
        new_todos_wire = [] if all_done else [_todo_to_wire(t) for t in todos]

        # Structural nudge: unreachable (guarded by a literal False below), so
        # verificationNudgeNeeded is always False.
        verification_nudge_needed = False
        if (
            False  # noqa: B002 - always dead; short-circuits the branch below
            and False
            and not context.agent_id
            and all_done
            and len(todos) >= 3
            and not any("verif" in t.content.lower() for t in todos)
        ):
            verification_nudge_needed = True

        def _update(prev: Any) -> Any:
            prev_todos = _get_app_state_todos(prev)
            if isinstance(prev, dict):
                return {**prev, "todos": {**prev_todos, todo_key: new_todos_wire}}
            # Non-dict app state: best-effort mutate-and-return.
            try:
                prev.todos = {**prev_todos, todo_key: new_todos_wire}
            except Exception:  # noqa: BLE001 - defensive; headless app state is a dict
                pass
            return prev

        if context.set_app_state:
            context.set_app_state(_update)

        return ToolResult(
            data={
                "oldTodos": old_todos,
                "newTodos": [_todo_to_wire(t) for t in todos],
                "verificationNudgeNeeded": verification_nudge_needed,
            }
        )

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        verification_nudge_needed = bool(data.get("verificationNudgeNeeded"))
        base = (
            "Todos have been modified successfully. Ensure that you continue to use the todo "
            "list to track your progress. Please proceed with the current tasks if applicable"
        )
        nudge = (
            (
                "\n\nNOTE: You just closed out 3+ tasks and none of them was a verification "
                "step. Before writing your final summary, spawn the verification agent "
                f'(subagent_type="{VERIFICATION_AGENT_TYPE}"). You cannot self-assign PARTIAL '
                "by listing caveats in your summary — only the verifier issues a verdict."
            )
            if verification_nudge_needed
            else ""
        )
        return {
            "tool_use_id": tool_use_id,
            "type": "tool_result",
            "content": base + nudge,
        }


# Singleton instance.
todo_write_tool = TodoWriteTool()
