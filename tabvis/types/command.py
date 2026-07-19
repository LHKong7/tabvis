"""Command / skill type contracts.

A Tabvis *command* is either a :class:`PromptCommand` (a "skill" — a prompt that expands into
content blocks) or a :class:`LocalCommand` (an imperative slash-command whose ``load()`` returns
a module exposing ``call``). Both share :class:`CommandBase` metadata. A command is a discriminated
union (``Command = CommandBase & (PromptCommand | LocalCommand)``), represented here as small
dataclasses with the async behavior carried as ``Callable`` / ``Protocol`` fields
(``get_prompt_for_command`` / ``call`` / ``load`` are async).

Casing convention (per ``docs/SPINE_CONTRACTS.md``): Python identifiers (dataclass fields, methods)
are snake_case; dict-shaped *data* that round-trips to JSON / the Anthropic API / the transcript
keeps its wire keys. A :data:`ContentBlockParam` therefore stays a plain ``dict`` (the content-block
form), not a typed model, and the :class:`LocalCommandResult` variant dicts keep camelCase keys
(``compactionResult`` / ``displayText``).

The command *registry* lives in ``tabvis/commands/__init__.py`` (mirroring how
``tabvis/tools/__init__.py`` hosts the tool registry). This module is only the *types*.

Some optional fields are kept as plain typed fields with clean-env defaults (``None``), so the
dataclass round-trips even though the backing subsystem is not implemented in this build:

* ``hooks`` (``HooksSettings``): hooks-on-skill registration.
* ``effort`` (``EffortValue``): effort gating.
* ``source`` literal includes the ``SettingSource`` values plus ``'builtin'`` / ``'mcp'`` /
  ``'bundled'`` — represented here as ``str``.
* :class:`LocalCommandResult`'s ``compact`` variant references ``CompactionResult``,
  represented here as an opaque ``Any`` payload.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Literal,
    Protocol,
    TypedDict,
    runtime_checkable,
)

if TYPE_CHECKING:
    from tabvis.tool import ToolUseContext

__all__ = [
    "Command",
    "CommandAvailability",
    "CommandBase",
    "CommandResultDisplay",
    "ContentBlockParam",
    "GetPromptForCommand",
    "LocalCommand",
    "LocalCommandCall",
    "LocalCommandLoad",
    "LocalCommandModule",
    "LocalCommandResult",
    "PromptCommand",
    "ResumeEntrypoint",
    "get_command_name",
    "is_command_enabled",
]


# ----------------------------------------------------------------------------------------------
# Wire-shaped aliases
# ----------------------------------------------------------------------------------------------

# A single Anthropic content-block param. Per SPINE_CONTRACTS content blocks round-trip as
# plain dicts with wire keys, so this is ``dict``.
ContentBlockParam = dict[str, Any]

# ``'userSettings' | 'projectSettings' | 'localSettings' | 'flagSettings' | 'policySettings'``;
# the PromptCommand ``source`` widens it with ``'builtin' | 'mcp' | 'bundled'``. Kept as ``str``
# until the settings subsystem is implemented.
CommandSource = str


# ----------------------------------------------------------------------------------------------
# LocalCommandResult (tagged union — wire-key data dicts)
# ----------------------------------------------------------------------------------------------


class LocalCommandTextResult(TypedDict):
    type: Literal["text"]
    value: str


class LocalCommandCompactResult(TypedDict, total=False):
    type: Literal["compact"]
    compactionResult: Any
    displayText: str


class LocalCommandSkipResult(TypedDict):
    type: Literal["skip"]


# ``{type:'text', value} | {type:'compact', compactionResult, displayText?} | {type:'skip'}``.
LocalCommandResult = (
    LocalCommandTextResult | LocalCommandCompactResult | LocalCommandSkipResult
)


# ----------------------------------------------------------------------------------------------
# Async callable signatures (these are runtime structural types — Protocols / Callable aliases)
# ----------------------------------------------------------------------------------------------


@runtime_checkable
class GetPromptForCommand(Protocol):
    """Async call signature ``(args, context) -> list[ContentBlockParam]`` for a skill prompt."""

    def __call__(
        self, args: str, context: ToolUseContext
    ) -> Awaitable[list[ContentBlockParam]]: ...


# ``(args, context) -> Awaitable[LocalCommandResult]`` — a local command implementation.
LocalCommandCall = Callable[[str, "ToolUseContext"], Awaitable["LocalCommandResult"]]


class LocalCommandModule(TypedDict):
    """Module shape returned by :attr:`LocalCommand.load` for lazy-loaded local commands."""

    call: LocalCommandCall


# ``() -> Awaitable[LocalCommandModule]`` — the lazy module loader for a local command.
LocalCommandLoad = Callable[[], Awaitable["LocalCommandModule"]]


# ----------------------------------------------------------------------------------------------
# CommandBase (shared metadata)
# ----------------------------------------------------------------------------------------------

# Declares which auth/provider environments a command is available in. Only ``'console'`` (direct
# API-key user) exists today; ``availability=None`` means "available everywhere".
CommandAvailability = Literal["console"]

CommandResultDisplay = Literal["skip", "system", "user"]

ResumeEntrypoint = Literal[
    "cli_flag",
    "slash_command_picker",
    "slash_command_session_id",
    "slash_command_title",
    "fork",
]


@dataclass
class CommandBase:
    """Shared command metadata.

    ``availability`` declares an auth/provider requirement (static: *who* may use the command),
    distinct from :meth:`is_enabled` (dynamic: is it turned on *right now*). Commands with no
    ``availability`` are available everywhere; commands with one are shown only when the user
    matches at least one listed auth type (see ``meets_availability_requirement`` in the registry).

    ``is_enabled_fn`` is an optional ``() -> bool`` override; the resolved default-``True``
    behavior is exposed via :meth:`is_enabled`. ``user_facing_name`` is an optional
    ``() -> str`` override for the displayed name; the resolution is :func:`get_command_name`.
    """

    name: str = ""
    description: str = ""
    availability: list[CommandAvailability] | None = None
    has_user_specified_description: bool | None = None
    # Defaults to true. Only set when the command has conditional enablement (flags / env checks).
    is_enabled_fn: Callable[[], bool] | None = None
    # Defaults to false. Only set when the command should be hidden from typeahead / help.
    is_hidden: bool | None = None
    aliases: list[str] | None = None
    is_mcp: bool | None = None
    # Hint text for command arguments (displayed in gray after the command).
    argument_hint: str | None = None
    # From the "Skill" spec — detailed usage scenarios for when to use this command.
    when_to_use: str | None = None
    version: str | None = None
    # Whether to disable this command from being invoked by models.
    disable_model_invocation: bool | None = None
    # Whether users can invoke this skill by typing /skill-name.
    user_invocable: bool | None = None
    # Where the command was loaded from.
    loaded_from: (
        Literal["commands_DEPRECATED", "skills", "managed", "bundled", "mcp"] | None
    ) = None
    # Distinguishes workflow-backed commands (badged in autocomplete).
    kind: Literal["workflow"] | None = None
    # If true, executes immediately without waiting for a stop point (bypasses queue).
    immediate: bool | None = None
    # If true, args are redacted from the conversation history.
    is_sensitive: bool | None = None
    # Defaults to ``name``. Only override when the displayed name differs.
    user_facing_name: Callable[[], str] | None = None

    def is_enabled(self) -> bool:
        """Resolve whether the command is enabled, defaulting to ``True``."""
        if self.is_enabled_fn is None:
            return True
        return self.is_enabled_fn()


# ----------------------------------------------------------------------------------------------
# PromptCommand ("skill")
# ----------------------------------------------------------------------------------------------


@dataclass
class PromptCommand(CommandBase):
    """A skill / prompt-command (``type:'prompt'``).

    ``get_prompt_for_command(args, context)`` is an async callable returning the list of
    :data:`ContentBlockParam` dicts the skill expands into. ``context`` is ``'inline'`` (default:
    the skill content expands into the current conversation) or ``'fork'`` (the skill runs as a
    sub-agent with separate context / token budget). ``agent`` is the agent type to fork as and is
    only meaningful when ``context == 'fork'``.
    """

    type: Literal["prompt"] = "prompt"
    progress_message: str = ""
    # Length of command content in characters (used for token estimation).
    content_length: int = 0
    arg_names: list[str] | None = None
    allowed_tools: list[str] | None = None
    model: str | None = None
    source: CommandSource = "builtin"
    disable_non_interactive: bool | None = None
    # Hook configuration applied when the skill is invoked. Opaque payload until the hooks
    # subsystem is implemented.
    hooks: Any = None
    # Base directory for skill resources.
    skill_root: str | None = None
    # Execution context: 'inline' (default) or 'fork' (run as sub-agent).
    context: Literal["inline", "fork"] | None = None
    # Agent type to use when forked (e.g. 'Bash', 'general-purpose'). Only used when context='fork'.
    agent: str | None = None
    effort: Any = None
    # Glob patterns for file paths this skill applies to. When set, the skill is only visible after
    # the model touches matching files.
    paths: list[str] | None = None
    # Async: (args, context) -> list[ContentBlockParam]. ``None`` until supplied by the loader.
    get_prompt_for_command: GetPromptForCommand | None = None


# ----------------------------------------------------------------------------------------------
# LocalCommand
# ----------------------------------------------------------------------------------------------


@dataclass
class LocalCommand(CommandBase):
    """A local imperative slash-command (``type:'local'``).

    ``load()`` is an async callable returning a :class:`LocalCommandModule` (``{'call': ...}``)
    whose ``call(args, context)`` performs the command and returns a :data:`LocalCommandResult`.
    ``supports_non_interactive`` gates availability in headless (``-p``) runs.
    """

    type: Literal["local"] = "local"
    supports_non_interactive: bool = False
    # Async: () -> LocalCommandModule. ``None`` until supplied.
    load: LocalCommandLoad | None = None


# ----------------------------------------------------------------------------------------------
# Command union + helpers
# ----------------------------------------------------------------------------------------------

# ``Command = CommandBase & (PromptCommand | LocalCommand)``. Both variants already extend
# :class:`CommandBase`, so the Python union is just the two concrete dataclasses.
Command = PromptCommand | LocalCommand


def get_command_name(cmd: CommandBase) -> str:
    """Resolve the user-visible name, falling back to ``cmd.name``.

    ``Cmd.userFacingName?.() ?? cmd.name``.
    """
    if cmd.user_facing_name is not None:
        return cmd.user_facing_name()
    return cmd.name


def is_command_enabled(cmd: CommandBase) -> bool:
    """Resolve whether the command is enabled, defaulting to ``True``.

    ``Cmd.isEnabled?.() ?? true`` (delegates to
    :meth:`CommandBase.is_enabled`).
    """
    return cmd.is_enabled()
