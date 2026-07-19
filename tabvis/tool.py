"""Tool contract, permission context, and runtime result mapping

The TS ``Tool`` is an object literal with many optional methods, assembled by ``buildTool``
which spreads ``TOOL_DEFAULTS``. The faithful Python analogue is a :class:`Tool` base class
whose default methods *are* ``TOOL_DEFAULTS``; concrete tools subclass it and are registered
as singleton instances. Zod input schemas become pydantic ``BaseModel`` subclasses.

Casing convention: Python identifiers (dataclass fields, methods) are snake_case; dict-shaped
*data* types that round-trip to JSON/the API/the transcript keep their original wire keys
(see ``tabvis.types.message`` / ``tabvis.types.permissions``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypedDict,
    TypeVar,
)

from tabvis.types.permissions import (
    PermissionResult,
    ToolPermissionContext,
    ToolPermissionRulesBySource,  # noqa: F401 - re-exported for parity
)
from tabvis.utils.abort import AbortController

if TYPE_CHECKING:
    from pydantic import BaseModel

    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.message import AssistantMessage, Message, ProgressMessage

T = TypeVar("T")


class ToolInputJSONSchema(TypedDict, total=False):
    type: str  # always 'object'
    properties: dict[str, Any]
    # Plus arbitrary extra keys.


@dataclass
class QueryChainTracking:
    chain_id: str
    depth: int


@dataclass
class ValidationResult:
    """``{ result: true }`` or ``{ result: false, message, errorCode }``."""

    result: bool
    message: str = ""
    error_code: int = 0


def get_empty_tool_permission_context() -> ToolPermissionContext:
    return ToolPermissionContext(
        mode="default",
        additionalWorkingDirectories={},
        alwaysAllowRules={},
        alwaysDenyRules={},
        alwaysAskRules={},
        isBypassPermissionsModeAvailable=False,
    )


# CompactProgressEvent: { type: 'hooks_start'|'compact_start'|'compact_end', ... }
CompactProgressEvent = dict[str, Any]


@dataclass
class ToolUseContextOptions:
    """The ``options`` bag of :class:`ToolUseContext`."""

    commands: list[Any] = field(default_factory=list)
    debug: bool = False
    main_loop_model: str = ""
    tools: Tools = field(default_factory=list)
    verbose: bool = False
    thinking_config: Any = None
    mcp_clients: list[Any] = field(default_factory=list)
    mcp_resources: dict[str, Any] = field(default_factory=dict)
    is_non_interactive_session: bool = False
    agent_definitions: Any = None
    max_budget_usd: float | None = None
    custom_system_prompt: str | None = None
    append_system_prompt: str | None = None
    query_source: str | None = None
    refresh_tools: Callable[[], Tools] | None = None


@dataclass
class ToolUseContext:
    """Execution context threaded through every tool call.

    Only the fields exercised by the headless spine are populated by default; UI-only
    callbacks are optional and ``None`` in headless/SDK contexts (as in the TS tree).
    """

    options: ToolUseContextOptions = field(default_factory=ToolUseContextOptions)
    abort_controller: AbortController = field(default_factory=AbortController)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    get_app_state: Callable[[], Any] = field(default=lambda: None)
    set_app_state: Callable[[Callable[[Any], Any]], None] = field(default=lambda _f: None)
    messages: list[Message] = field(default_factory=list)

    # Optional / context-dependent fields (UI, subagents, budgets).
    set_app_state_for_tasks: Callable[[Callable[[Any], Any]], None] | None = None
    handle_elicitation: Callable[..., Awaitable[Any]] | None = None
    add_notification: Callable[[dict[str, Any]], None] | None = None
    append_system_message: Callable[[Any], None] | None = None
    send_os_notification: Callable[[dict[str, Any]], None] | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    tool_use_id: str | None = None
    user_modified: bool | None = None
    require_can_use_tool: bool | None = None
    set_in_progress_tool_use_ids: Callable[[Callable[[set[str]], set[str]]], None] | None = None
    set_response_length: Callable[[Callable[[int], int]], None] | None = None
    update_file_history_state: Callable[[Callable[[Any], Any]], None] | None = None
    update_attribution_state: Callable[[Callable[[Any], Any]], None] | None = None
    set_conversation_id: Callable[[str], None] | None = None
    file_reading_limits: dict[str, Any] | None = None
    glob_limits: dict[str, Any] | None = None
    tool_decisions: dict[str, Any] | None = None
    query_tracking: QueryChainTracking | None = None
    request_prompt: Callable[..., Any] | None = None
    content_replacement_state: Any = None
    rendered_system_prompt: Any = None


# Progress callback plumbing.
@dataclass
class ToolProgress(Generic[T]):
    tool_use_id: str
    data: T


ToolCallProgress = Callable[[ToolProgress[Any]], None]


def filter_tool_progress_messages(
    progress_messages: Sequence[ProgressMessage],
) -> list[ProgressMessage]:
    return [
        msg
        for msg in progress_messages
        if (msg.get("data") or {}).get("type") != "hook_progress"
    ]


@dataclass
class ToolResult(Generic[T]):
    """Result returned from :meth:`Tool.call`."""

    data: T
    new_messages: list[Message] | None = None
    # context_modifier is only honored for tools that aren't concurrency safe.
    context_modifier: Callable[[ToolUseContext], ToolUseContext] | None = None
    # MCP protocol metadata (structuredContent, _meta) passed through to SDK consumers.
    mcp_meta: dict[str, Any] | None = None


def tool_matches_name(tool: Tool, name: str) -> bool:
    """Whether ``tool`` matches ``name`` by primary name or alias."""
    return tool.name == name or (name in (tool.aliases or []))


def find_tool_by_name(tools: Tools, name: str) -> Tool | None:
    for t in tools:
        if tool_matches_name(t, name):
            return t
    return None


class Tool:
    """Base tool contract. Concrete tools subclass and override the required members.

    Required to override: :attr:`name`, :attr:`input_schema`, :attr:`max_result_size_chars`,
    :meth:`call`, :meth:`description`, :meth:`prompt`,
    :meth:`map_tool_result_to_tool_result_block_param`.

    Defaults below mirror ``TOOL_DEFAULTS``/``buildTool`` (fail-closed where it matters).
    """

    # --- identity / discovery ---
    name: str = ""
    aliases: list[str] | None = None
    search_hint: str | None = None

    # --- schema ---
    input_schema: type[BaseModel]  # pydantic model class (was a Zod schema)
    input_json_schema: ToolInputJSONSchema | None = None
    output_schema: type[BaseModel] | None = None

    # --- result persistence / API hints ---
    max_result_size_chars: float = 0
    should_defer: bool = False
    always_load: bool = False
    strict: bool = False
    is_mcp: bool = False
    is_lsp: bool = False
    mcp_info: dict[str, str] | None = None

    # --- required behavior ---
    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: ToolCallProgress | None = None,
    ) -> ToolResult[Any]:
        raise NotImplementedError(f"{self.name}.call is not implemented")

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        raise NotImplementedError(f"{self.name}.description is not implemented")

    async def prompt(self, options: dict[str, Any]) -> str:
        raise NotImplementedError(f"{self.name}.prompt is not implemented")

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError(
            f"{self.name}.map_tool_result_to_tool_result_block_param is not implemented"
        )

    # --- defaultable methods (TOOL_DEFAULTS) ---
    def is_enabled(self) -> bool:
        return True

    def is_concurrency_safe(self, input: Any) -> bool:
        return False

    def is_read_only(self, input: Any) -> bool:
        return False

    def is_destructive(self, input: Any) -> bool:
        return False

    async def check_permissions(
        self, input: Any, context: ToolUseContext
    ) -> PermissionResult:
        # Defer to the general permission system.
        return {"behavior": "allow", "updatedInput": input}

    def user_facing_name(self, input: Any | None = None) -> str:
        return self.name

    # --- optional behavior (present on some tools) ---
    def inputs_equivalent(self, a: Any, b: Any) -> bool:  # pragma: no cover - optional
        return a == b

    def interrupt_behavior(self) -> str:  # 'cancel' | 'block'
        return "block"

    def is_search_or_read_command(self, input: Any) -> dict[str, bool] | None:
        return None

    def is_open_world(self, input: Any) -> bool:
        return False

    def requires_user_interaction(self) -> bool:
        return False

    def backfill_observable_input(self, input: dict[str, Any]) -> None:
        return None

    async def validate_input(
        self, input: Any, context: ToolUseContext
    ) -> ValidationResult:
        return ValidationResult(result=True)

    def get_path(self, input: Any) -> str | None:
        return None

    async def prepare_permission_matcher(
        self, input: Any
    ) -> Callable[[str], bool] | None:
        return None

    def get_tool_use_summary(self, input: Any | None) -> str | None:
        return None

    def get_activity_description(self, input: Any | None) -> str | None:
        return None

    def is_transparent_wrapper(self) -> bool:
        return False

    def extract_search_text(self, out: Any) -> str | None:
        return None

    def is_result_truncated(self, output: Any) -> bool:
        return False


# A collection of tools (parity with the TS `Tools` alias for `readonly Tool[]`).
Tools = Sequence[Tool]
