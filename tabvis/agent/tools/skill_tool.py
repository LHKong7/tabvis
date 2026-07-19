"""Skill tool — invoke a slash-command *skill* (prompt-command).

A "skill" in Tabvis is a PROMPT-COMMAND (:class:`tabvis.types.command.PromptCommand`): a named prompt
that *expands* into Anthropic content blocks the main loop then processes. The Skill tool is the
model-facing wrapper that lets the model invoke one by name (``{skill, args?}``). Most bundled
skills self-gate on user type, so the set of skills discoverable in a given build is only whatever
applies to that build's users.

:class:`SkillTool` subclasses :class:`tabvis.tool.Tool` and is exported as the singleton
:data:`skill_tool`. Its input schema is the pydantic v2 ``BaseModel`` :class:`SkillToolInput`
(``{skill: str, args?: str}``).

:meth:`SkillTool.call`:

* normalizes the skill name (strips a leading ``/``),
* resolves it via :func:`tabvis.ui.commands.get_all_commands` / :func:`~tabvis.ui.commands.find_command`,
* validates it exists, is a prompt-command, and is NOT ``disable_model_invocation`` (returning a
  clear ``is_error`` tool_result otherwise),
* awaits ``command.get_prompt_for_command(args, context)`` to get the expanded content blocks, and
* returns ``ToolResult(data={"content": <blocks or their joined text>, "skill": name})``.

``map_tool_result_to_tool_result_block_param`` then renders ``{type:'tool_result', tool_use_id,
content: <expanded skill prompt text>}``.

Not supported, all with clean-env defaults:

* **fork-context execution** — running a skill as an isolated sub-agent when
  ``command.context == 'fork'``. Not supported; ``context`` is always treated as inline.
* **effort** — skill effort override in the ``contextModifier``. Not supported.
* **permission suggestions** — allow/deny-rule matching with an ``ask`` fallback and rule-add
  suggestions. This tool allows when no deny rule matches; suggestions are not emitted.
* **skill hooks registration** — not supported; expanding the prompt does not register hooks.
* the full prompt-slash-command pipeline (allowed-tools / model-override context modifiers,
  tool-use-id tagging, new-message tracking, invocation analytics). This tool returns the expanded
  prompt content directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from tabvis.ui.commands import find_command, get_all_commands
from tabvis.tool import Tool, ToolResult, ToolUseContext, ValidationResult
from tabvis.types.command import Command, ContentBlockParam, PromptCommand
from tabvis.utils.debug import log_for_debugging

if TYPE_CHECKING:
    from tabvis.types.can_use_tool import CanUseToolFn
    from tabvis.types.message import AssistantMessage

# Tool name as exposed to the model.
SKILL_TOOL_NAME = "Skill"

# Hardcoded here rather than pulling in a separate xml-tags module for one referenced string value.
COMMAND_NAME_TAG = "command-name"

# Per-entry hard cap for the discovery listing.
# The listing is for discovery only — the Skill tool loads full content on invoke.
MAX_LISTING_DESC_CHARS = 250


# ----------------------------------------------------------------------------------------------
# Input schema
# ----------------------------------------------------------------------------------------------


class SkillToolInput(BaseModel):
    """Validated input for :data:`skill_tool`.

    ``{skill: str, args?: str}`` — ``skill`` is the skill name (a leading ``/`` is tolerated and
    stripped by the tool), ``args`` are optional arguments forwarded to the skill prompt.
    """

    model_config = ConfigDict(extra="forbid")

    skill: str = Field(
        description='The skill name. E.g., "commit", "review-pr", or "pdf"',
    )
    args: str | None = Field(
        default=None,
        description="Optional arguments for the skill",
    )


# ----------------------------------------------------------------------------------------------
# Name normalization + prompt listing
# ----------------------------------------------------------------------------------------------


def normalize_skill_name(skill: str) -> str:
    """Trim whitespace and strip a single leading ``/``.

    The model may pass either ``"commit"`` or ``"/commit"``; both resolve to the bare command name.
    """
    trimmed = skill.strip()
    return trimmed[1:] if trimmed.startswith("/") else trimmed


def _command_description(cmd: Command) -> str:
    """``description`` joined with ``when_to_use`` then capped to :data:`MAX_LISTING_DESC_CHARS`."""
    when_to_use = getattr(cmd, "when_to_use", None)
    desc = f"{cmd.description} - {when_to_use}" if when_to_use else cmd.description
    if len(desc) > MAX_LISTING_DESC_CHARS:
        return desc[: MAX_LISTING_DESC_CHARS - 1] + "…"
    return desc


def format_available_skills(commands: list[Command]) -> str:
    """Render the available skills as ``- name: description`` lines (bounded discovery listing).

    Each entry is capped at :data:`MAX_LISTING_DESC_CHARS` characters; there is no overall
    context-budget accounting here since the listing is expected to stay small.
    """
    lines = [f"- {cmd.name}: {_command_description(cmd)}" for cmd in commands]
    return "\n".join(lines)


def get_prompt(commands: list[Command]) -> str:
    """The Skill tool prompt — fixed guidance + a bounded listing of available skills.

    The available skill list is normally surfaced via system-reminder messages rather than
    inline; this function appends the listing directly so the returned prompt is useful
    standalone.
    """
    listing = format_available_skills(commands)
    available = (
        f"\n\nAvailable skills:\n{listing}" if listing else ""
    )
    return (
        "Execute a skill within the main conversation\n"
        "\n"
        "When users ask you to perform tasks, check if any of the available skills match. "
        "Skills provide specialized capabilities and domain knowledge.\n"
        "\n"
        'When users reference a "slash command" or "/<something>" (e.g., "/commit", '
        '"/review-pr"), they are referring to a skill. Use this tool to invoke it.\n'
        "\n"
        "How to invoke:\n"
        "- Use this tool with the skill name and optional arguments\n"
        "- Examples:\n"
        '  - `skill: "pdf"` - invoke the pdf skill\n'
        "  - `skill: \"commit\", args: \"-m 'Fix bug'\"` - invoke with arguments\n"
        '  - `skill: "review-pr", args: "123"` - invoke with arguments\n'
        '  - `skill: "ms-office-suite:pdf"` - invoke using fully qualified name\n'
        "\n"
        "Important:\n"
        "- Available skills are listed in system-reminder messages in the conversation\n"
        "- When a skill matches the user's request, this is a BLOCKING REQUIREMENT: invoke the "
        "relevant Skill tool BEFORE generating any other response about the task\n"
        "- NEVER mention a skill without actually calling this tool\n"
        "- Do not invoke a skill that is already running\n"
        "- Do not use this tool for built-in CLI commands (like /help, /clear, etc.)\n"
        f"- If you see a <{COMMAND_NAME_TAG}> tag in the current conversation turn, the skill has "
        "ALREADY been loaded - follow the instructions directly instead of calling this tool "
        "again\n" + available
    )


# ----------------------------------------------------------------------------------------------
# Resolution + content-block flattening helpers
# ----------------------------------------------------------------------------------------------


def _resolve_skill(name: str, context: ToolUseContext | None) -> Command | None:
    """Find the command named ``name`` among all commands visible to ``context``."""
    commands = get_all_commands(context)
    return find_command(name, commands)


def blocks_to_text(blocks: list[ContentBlockParam]) -> str:
    """Join the ``text`` of content blocks into a single string.

    Only ``{type:'text', text}`` blocks contribute text (non-text blocks have no textual rendering
    here); blocks are joined with newlines.
    """
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


# ----------------------------------------------------------------------------------------------
# Tool
# ----------------------------------------------------------------------------------------------


class SkillTool(Tool):
    """``Skill`` — invoke a slash-command skill (prompt-command) by name."""

    name = SKILL_TOOL_NAME
    search_hint = "invoke a slash-command skill"
    input_schema = SkillToolInput
    # 100K chars — an expanded skill prompt can be large.
    max_result_size_chars = 100_000

    def is_concurrency_safe(self, input: Any) -> bool:
        # Only one skill expands at a time: the tool turns the command into a full prompt that the
        # main loop must process before continuing.
        return False

    def is_read_only(self, input: Any) -> bool:
        # Expanding a skill prompt has no side effects on disk by itself.
        return True

    async def description(self, input: Any, options: dict[str, Any]) -> str:
        skill = (
            input.get("skill")
            if isinstance(input, dict)
            else getattr(input, "skill", "")
        )
        return f"Execute skill: {skill}"

    async def prompt(self, options: dict[str, Any]) -> str:
        commands = get_all_commands(None)
        return get_prompt(commands)

    async def validate_input(self, input: Any, context: ToolUseContext) -> ValidationResult:
        skill = input.get("skill") if isinstance(input, dict) else getattr(input, "skill", "")
        trimmed = (skill or "").strip()
        if not trimmed:
            return ValidationResult(
                result=False, message=f"Invalid skill format: {skill}", error_code=1
            )

        name = normalize_skill_name(trimmed)
        found = _resolve_skill(name, context)
        if found is None:
            return ValidationResult(
                result=False, message=f"Unknown skill: {name}", error_code=2
            )
        if found.disable_model_invocation:
            return ValidationResult(
                result=False,
                message=(
                    f"Skill {name} cannot be used with {SKILL_TOOL_NAME} tool due to "
                    "disable-model-invocation"
                ),
                error_code=4,
            )
        if not isinstance(found, PromptCommand) or found.type != "prompt":
            return ValidationResult(
                result=False,
                message=f"Skill {name} is not a prompt-based skill",
                error_code=5,
            )
        return ValidationResult(result=True)

    def map_tool_result_to_tool_result_block_param(
        self, content: Any, tool_use_id: str
    ) -> dict[str, Any]:
        data = content if isinstance(content, dict) else {}
        body = data.get("content")
        if isinstance(body, list):
            text = blocks_to_text(body)
        elif body is None:
            text = ""
        else:
            text = str(body)
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": text,
        }
        if data.get("is_error"):
            block["is_error"] = True  # surface skill failures to the model
        return block

    async def call(
        self,
        args: Any,
        context: ToolUseContext,
        can_use_tool: CanUseToolFn,
        parent_message: AssistantMessage,
        on_progress: Any = None,
    ) -> ToolResult[Any]:
        skill = args.skill if not isinstance(args, dict) else args.get("skill", "")
        skill_args = args.args if not isinstance(args, dict) else args.get("args")

        name = normalize_skill_name(skill or "")

        # Resolve + re-validate existence / type / gate (defensive: validate_input may be bypassed
        # by direct callers). Each failure returns a clear is_error tool_result.
        command = _resolve_skill(name, context)
        if command is None:
            return self._error_result(f"Unknown skill: {name}", tool_use_id=parent_message)
        if command.disable_model_invocation:
            return self._error_result(
                f"Skill {name} cannot be used with {SKILL_TOOL_NAME} tool due to "
                "disable-model-invocation",
                tool_use_id=parent_message,
            )
        if not isinstance(command, PromptCommand) or command.type != "prompt":
            return self._error_result(
                f"Skill {name} is not a prompt-based skill", tool_use_id=parent_message
            )

        if command.get_prompt_for_command is None:
            return self._error_result(
                f"Skill {name} has no prompt to expand", tool_use_id=parent_message
            )

        if command.context == "fork":
            log_for_debugging(
                f"SkillTool: fork-context skill {name!r} executed inline (fork path not supported)."
            )

        blocks = await command.get_prompt_for_command(skill_args or "", context)

        log_for_debugging(f"SkillTool expanded skill {name} into {len(blocks)} content block(s)")

        return ToolResult(data={"content": blocks, "skill": name})

    def _error_result(self, message: str, tool_use_id: Any) -> ToolResult[Any]:
        """Build an is_error :class:`ToolResult` carrying a tool_result block.

        ``tool_use_id`` accepts the parent message; the message text alone is sufficient
        downstream, so the id itself is not threaded through here.
        """
        return ToolResult(
            data={"content": message, "skill": None, "is_error": True},
        )


skill_tool = SkillTool()
