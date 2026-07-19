"""Command / skill registry

PACKAGE COLLISION NOTE: in TS, ``src/commands.ts`` is the *registry* and ``src/commands/`` is a
*directory* of built-in command modules. In Python a module and a package of the same name collide,
so — exactly as ``tabvis/tools/__init__.py`` hosts the tool registry (was ``src/tools.ts``) — the
command *registry* lives here in ``tabvis/commands/__init__.py``. The command *types* live in
:mod:`tabvis.types.command`.

What this module aggregates (the three command sources of ``getCommands``):

* directory skills — :func:`tabvis.agent.skills.load_skills_dir.load_skills_dir` (``<cwd>/.tabvis/skills`` +
  ``~/.tabvis/skills``).
* built-in commands — :func:`COMMANDS`. For headless ``tabvis -p`` the UI slash-commands
  (``/clear``, ``/compact``, ``/cost``, ``/init``, ``/review`` …) are removed, so this is an empty
  list. The TS ``USER_TYPE === 'ant' && !IS_DEMO`` ``INTERNAL_ONLY_COMMANDS`` gate is implemented as the
  registration *mechanism* (also empty here — no built-in command module is implemented).
* MCP skill commands — threaded separately through :func:`get_all_commands` from the tool-use
  context (see :func:`get_mcp_skill_commands`; the MCP-command source itself is a clean-env stub).

Bounded-scope stubs, all with clean-env defaults:

* Dynamic skills (``getDynamicSkills`` — skills discovered during file ops): the clean-env set is
  empty, so :func:`get_commands` returns just the base commands (no insert-before-builtins logic).
* Workflow commands (``loadWorkflowCommandSpecs`` / ``workflowSpecToCommand``): not implemented — no
  workflow commands are aggregated.
* Availability gating beyond default: :func:`meets_availability_requirement` returns ``True`` for
  the only ``'console'`` requirement in the clean (non-3P, non-gateway) tree — the
  ``isUsing3PServices`` / ``isFirstPartyProviderBaseUrl`` probes are stubbed to that default.
* Memoization (``memoize`` by cwd): dropped — loading is cheap in headless and re-evaluating fresh
  matches the TS contract that availability/``isEnabled`` run on every call.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tabvis.agent.skills.load_skills_dir import load_skills_dir
from tabvis.types.command import (
    Command,
    PromptCommand,
    get_command_name,
    is_command_enabled,
)
from tabvis.utils.cwd import get_cwd
from tabvis.utils.debug import log_for_debugging
from tabvis.utils.env_utils import is_env_truthy
from tabvis.utils.log import log_error

if TYPE_CHECKING:
    from tabvis.tool import ToolUseContext

__all__ = [
    "COMMANDS",
    "INTERNAL_ONLY_COMMANDS",
    "built_in_command_names",
    "find_command",
    "get_all_commands",
    "get_command",
    "get_command_name",
    "get_commands",
    "get_mcp_skill_commands",
    "has_command",
    "is_command_enabled",
    "meets_availability_requirement",
]


# ----------------------------------------------------------------------------------------------
# Built-in commands (the TS ``src/commands/`` directory modules)
# ----------------------------------------------------------------------------------------------

# Commands eliminated from the external build (``USER_TYPE === 'ant' && !IS_DEMO``). In TS these are
# ``[commit, commitPushPr, initVerifiers, version]``; none of those imperative slash-command modules
# are implemented for the headless skeleton, so the gated set is empty while the gate remains active.
INTERNAL_ONLY_COMMANDS: list[Command] = []


def _is_demo() -> bool:
    return is_env_truthy(os.environ.get("IS_DEMO"))


def COMMANDS() -> list[Command]:  # noqa: N802 - registry name mirrors the TS export `COMMANDS`
    """Built-in commands.

    Declared as a function (not a module constant) because the underlying TS commands read from
    config, which can't be read at import time. For headless ``tabvis -p`` the UI slash-commands are
    removed, so the base list is empty; only the ``USER_TYPE === 'ant' && !IS_DEMO`` internal-only
    gate is reproduced (also empty here — see :data:`INTERNAL_ONLY_COMMANDS`).
    """
    from tabvis.ui.commands.dynamic_workflow import dynamic_workflow

    # dynamic-workflow supports non-interactive (-p) use, so it ships in the headless base set.
    base: list[Command] = [dynamic_workflow]
    return base


def built_in_command_names() -> set[str]:
    """Set of every built-in command name + alias."""
    names: set[str] = set()
    for cmd in COMMANDS():
        names.add(cmd.name)
        names.update(cmd.aliases or [])
    return names


# ----------------------------------------------------------------------------------------------
# Availability gating (static auth/provider requirement, distinct from isEnabled)
# ----------------------------------------------------------------------------------------------


def _is_using_3p_services() -> bool:
    # customer (not a configured service / gateway user), so this is False.
    return False


def _is_first_party_provider_base_url() -> bool:
    # first-party base URL the skeleton targets.
    return True


def meets_availability_requirement(cmd: Command) -> bool:
    """Filter a command by its declared ``availability``.

    Commands without an ``availability`` requirement are universal. The only requirement today is
    ``'console'`` (direct 1P API-key user, not a 3P/gateway user). Runs before ``is_enabled`` so
    provider-gated commands are hidden regardless of feature-flag state. NOT memoized — auth state
    can change mid-session, so it is re-evaluated on every :func:`get_commands` call.
    """
    if not cmd.availability:
        return True
    for requirement in cmd.availability:
        if requirement == "console":
            if not _is_using_3p_services() and _is_first_party_provider_base_url():
                return True
    return False


# ----------------------------------------------------------------------------------------------
# Skill aggregation
# ----------------------------------------------------------------------------------------------


def _get_directory_skills(cwd: str) -> list[Command]:
    """Load project and user directory skills without making failures fatal."""
    try:
        skill_dir_commands: list[Command] = list(load_skills_dir(cwd))
    except Exception as err:  # noqa: BLE001 - skills are non-critical; never break the registry
        log_error(err)
        log_for_debugging(
            "Skill directory commands failed to load, continuing without them",
        )
        skill_dir_commands = []

    log_for_debugging(
        f"get_directory_skills returning: {len(skill_dir_commands)} skill dir commands",
    )
    return skill_dir_commands


def _load_all_commands(cwd: str) -> list[Command]:
    """Load every command source: directory skills, workflows, and built-in commands."""
    skill_dir_commands = _get_directory_skills(cwd)
    workflow_commands = _get_workflow_commands(cwd)
    return [
        *skill_dir_commands,
        *workflow_commands,
        *COMMANDS(),
    ]


def _get_workflow_commands(cwd: str) -> list[Command]:
    """Load saved ``/<name>`` workflow commands (personal + project dirs).

    Each saved Python workflow under ``~/.tabvis/workflows`` / ``.tabvis/workflows`` becomes a
    ``/<slug>`` command that re-runs the script. Failures are non-critical — logged and swallowed so a malformed
    workflow never breaks the command registry.
    """
    try:
        from tabvis.agent.workflows.commands import workflow_spec_to_command
        from tabvis.agent.workflows.storage import load_workflow_command_specs_sync

        return [workflow_spec_to_command(spec) for spec in load_workflow_command_specs_sync(cwd)]
    except Exception as err:  # noqa: BLE001 - workflows are non-critical; never break the registry
        log_error(err)
        log_for_debugging("Workflow commands failed to load, continuing without them")
        return []


def get_commands(cwd: str | None = None) -> list[Command]:
    """Return commands available to the current user.

    Aggregates directory skills + workflow and built-in commands, then filters by
    :func:`meets_availability_requirement` and :func:`is_command_enabled` (both re-evaluated fresh,
    not memoized, so auth/flag changes take effect immediately). The TS ``getDynamicSkills`` insert
    pass is a no-op stub here (the clean-env dynamic-skill set is empty), so the base list is
    returned directly.
    """
    cwd = cwd or get_cwd()
    all_commands = _load_all_commands(cwd)

    base_commands = [
        cmd
        for cmd in all_commands
        if meets_availability_requirement(cmd) and is_command_enabled(cmd)
    ]

    # operations are inserted after loaded skills/workflows but before built-ins. The clean-env set
    # is empty, so there is nothing to insert.
    return base_commands


# ----------------------------------------------------------------------------------------------
# MCP skill commands (threaded separately through get_all_commands)
# ----------------------------------------------------------------------------------------------


def get_mcp_skill_commands(mcp_commands: list[Command]) -> list[Command]:
    """Filter context-provided MCP commands to model-invocable prompt skills.

    MCP skills live outside :func:`get_commands` (they come from ``AppState.mcp.commands``), so
    callers that need them in a skill index thread them through here. The TS body currently returns
    ``[]`` (the filtering is a placeholder), so this faithfully returns ``[]`` regardless of input.
    """
    # placeholder returning [] in the TS source too — kept faithful.
    return []


def _mcp_commands_from_context(context: ToolUseContext | None) -> list[Command]:
    """Extract MCP-provided commands from a tool-use context (clean-env stub).

    The MCP command source is ``AppState.mcp.commands``; the headless skeleton has no MCP commands
    registered, so this returns ``[]`` unless a context happens to carry them on
    ``options.commands``.
    """
    if context is None:
        return []
    commands = getattr(getattr(context, "options", None), "commands", None) or []
    return [c for c in commands if isinstance(c, PromptCommand) and c.loaded_from == "mcp"]


def get_all_commands(context: ToolUseContext | None = None) -> list[Command]:
    """All commands a tool-use context can see: directory skills + MCP skill commands.

    Convenience aggregator over :func:`get_commands` (the cwd-loaded set) plus any MCP skill
    commands carried on ``context`` (via :func:`get_mcp_skill_commands`). The MCP source is a
    clean-env stub, so in the restored tree this equals :func:`get_commands`.
    """
    cwd = get_cwd()
    base = get_commands(cwd)
    mcp_skill_commands = get_mcp_skill_commands(_mcp_commands_from_context(context))
    return [*base, *mcp_skill_commands]


# ----------------------------------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------------------------------


def find_command(command_name: str, commands: list[Command]) -> Command | None:
    """Find a command by name / user-facing name / alias.

    A leading ``/`` is stripped first so slash-prefixed input (``/my-skill``) matches the bare
    command name (``my-skill``).
    """
    name = command_name[1:] if command_name.startswith("/") else command_name
    for cmd in commands:
        if (
            cmd.name == name
            or get_command_name(cmd) == name
            or (cmd.aliases is not None and name in cmd.aliases)
        ):
            return cmd
    return None


def has_command(command_name: str, commands: list[Command]) -> bool:
    """Whether a command exists."""
    return find_command(command_name, commands) is not None


def get_command(command_name: str, commands: list[Command]) -> Command:
    """Find a command or raise (``ReferenceError`` -> ``LookupError``)."""
    command = find_command(command_name, commands)
    if command is None:
        available = sorted(
            (
                f"{get_command_name(c)} (aliases: {', '.join(c.aliases)})"
                if c.aliases
                else get_command_name(c)
            )
            for c in commands
        )
        raise LookupError(
            f"Command {command_name} not found. Available commands: {', '.join(available)}"
        )
    return command
