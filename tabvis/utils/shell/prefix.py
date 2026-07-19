"""Shared command prefix extraction using Haiku

Factory for command-prefix extractors used by different shell tools. The core logic (Haiku query,
response validation) is shared; tool-specific aspects (examples, pre-checks) are configurable.

Implementation notes (per ``docs/SPINE_CONTRACTS.md`` + the FLAT ``tabvis/tools`` architecture):
- ``memoizeWithLRU`` → :func:`tabvis.utils.memoize.memoize_with_lru`. The TS two-layer pattern (the
  inner factory returns a promise + attaches a ``.catch`` that evicts on rejection, identity-guarded
  against a newer entry) is preserved: the memoized cell stores the coroutine-producing call's
  result, and we add an eviction wrapper via ``asyncio.ensure_future``-style done-callback on the task
  returned to the caller. Since the cache key is the command string only, the ``memoized.cache``
  handle (``.get`` / ``.delete``) is used for the identity-guarded eviction.
- ``jsonStringify`` → :func:`tabvis.utils.slow_operations.json_stringify`;
  ``asSystemPrompt`` → :func:`tabvis.utils.system_prompt_type.as_system_prompt`;
  ``startsWithApiErrorPrefix`` → :func:`tabvis.agent.api.errors.starts_with_api_error_prefix`.
- ``chalk.yellow(...)`` → a local SGR yellow wrap (house style — no chalk dep); the non-interactive
  branch writes a JSON line to stderr via ``json_stringify``.
- ``process.env.NODE_ENV === 'test'`` → ``os.environ.get('NODE_ENV') == 'test'``;
  ``Date.now()`` → wall-clock ms; ``setTimeout`` / ``clearTimeout`` → an asyncio timer handle that
  fires the slow-preflight warning after 10s and is cancelled when the query returns.
- Command-prefix extraction via Haiku is not supported in this build. Calling the extractor at
  runtime raises a clear ``NotImplementedError``.
- ``AbortSignal`` → :class:`tabvis.utils.abort.AbortSignal`.
- ``CommandPrefixResult`` / ``CommandSubcommandPrefixResult`` round-trip only within the runtime
  (never to JSON/the transcript) → modelled as ``TypedDict`` / plain dicts. The ``Map<str, ...>``
  of subcommand prefixes → a plain ``dict``.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from tabvis.constants.query_source import QuerySource
from tabvis.agent.api.errors import starts_with_api_error_prefix
from tabvis.utils.abort import AbortSignal
from tabvis.utils.memoize import memoize_with_lru
from tabvis.utils.slow_operations import json_stringify
from tabvis.utils.system_prompt_type import as_system_prompt

# Shell executables that must never be accepted as bare prefixes. Allowing e.g. "bash:*" would let
# any command through, defeating the permission system. Includes Unix shells and Windows equivalents.
DANGEROUS_SHELL_PREFIXES: set[str] = {
    "sh",
    "bash",
    "zsh",
    "fish",
    "csh",
    "tcsh",
    "ksh",
    "dash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "bash.exe",
}


class CommandPrefixResult(TypedDict):
    """Result of command prefix extraction.

    ``commandPrefix`` (wire-shaped camelCase) is the detected prefix, or ``None`` if none could be
    determined.
    """

    commandPrefix: str | None


# CommandSubcommandPrefixResult = CommandPrefixResult + a {subcommand: CommandPrefixResult} map.
# Modelled as a plain dict at runtime with keys ``commandPrefix`` + ``subcommandPrefixes``.
CommandSubcommandPrefixResult = dict[str, Any]


class PrefixExtractorConfig(TypedDict, total=False):
    """Configuration for creating a command prefix extractor."""

    toolName: str
    policySpec: str
    eventName: str
    querySource: QuerySource
    preCheck: Callable[[str], CommandPrefixResult | None] | None


def _chalk_yellow(text: str) -> str:
    """Local stand-in for ``chalk.yellow`` (SGR yellow foreground)."""
    return f"\x1b[33m{text}\x1b[39m"


def _now_ms() -> int:
    return int(time.time() * 1000)


class _LazyTask:
    """A shared, multiple-await-safe awaitable over a single coroutine.

    The TS memoized cell stores an eagerly-started ``Promise`` (the impl runs immediately) plus a
    ``.catch`` eviction handler. CPython coroutines can only be awaited once, and ``ensure_future``
    needs a running loop, so this wrapper defers task creation until the FIRST await (when a loop is
    guaranteed to be running) and lets every subsequent await share the same :class:`asyncio.Task`.
    On task failure/cancellation it runs ``on_done`` (the identity-guarded cache eviction).
    """

    __slots__ = ("_coro_factory", "_on_done", "_task")

    def __init__(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        on_done: Callable[[asyncio.Task[Any]], None],
    ) -> None:
        self._coro_factory = coro_factory
        self._on_done = on_done
        self._task: asyncio.Task[Any] | None = None

    def _ensure_task(self) -> asyncio.Task[Any]:
        if self._task is None:
            task = asyncio.ensure_future(self._coro_factory())
            task.add_done_callback(self._on_done)
            self._task = task
        return self._task

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._ensure_task().__await__()


def create_command_prefix_extractor(
    config: PrefixExtractorConfig,
) -> Callable[[str, AbortSignal, bool], Awaitable[CommandPrefixResult | None]]:
    """Create a memoized command prefix extractor.

    Uses two-layer memoization: the outer memoized cell stores the coroutine task and attaches a
    done-callback that evicts the cache entry on failure (identity-guarded so a newer entry for the
    same key is not clobbered). Bounded to 200 entries via LRU.
    """
    tool_name = config["toolName"]
    policy_spec = config["policySpec"]
    event_name = config["eventName"]
    query_source = config["querySource"]
    pre_check = config.get("preCheck")

    def _factory(
        command: str,
        abort_signal: AbortSignal,
        is_non_interactive_session: bool,
    ) -> Awaitable[CommandPrefixResult | None]:
        # Evict on rejection so aborted calls don't poison future turns. Identity guard: after LRU
        # eviction a newer entry may occupy this key; a stale rejection must not delete it.
        def _on_done(t: asyncio.Task[CommandPrefixResult | None]) -> None:
            if t.cancelled() or t.exception() is not None:
                if memoized.cache.get(command) is lazy:  # type: ignore[attr-defined]
                    memoized.cache.delete(command)  # type: ignore[attr-defined]

        lazy = _LazyTask(
            lambda: _get_command_prefix_impl(
                command,
                abort_signal,
                is_non_interactive_session,
                tool_name,
                policy_spec,
                event_name,
                query_source,
                pre_check,
            ),
            _on_done,
        )
        return lazy

    memoized = memoize_with_lru(
        _factory,
        lambda command, *_args: command,  # memoize by command only
        200,
    )
    return memoized


def create_subcommand_prefix_extractor(
    get_prefix: Callable[[str, AbortSignal, bool], Awaitable[CommandPrefixResult | None]],
    split_command: Callable[[str], list[str] | Awaitable[list[str]]],
) -> Callable[[str, AbortSignal, bool], Awaitable[CommandSubcommandPrefixResult | None]]:
    """Create a memoized compound-command prefix extractor.

    Same two-layer memoization as :func:`create_command_prefix_extractor`.
    """

    def _factory(
        command: str,
        abort_signal: AbortSignal,
        is_non_interactive_session: bool,
    ) -> Awaitable[CommandSubcommandPrefixResult | None]:
        def _on_done(t: asyncio.Task[CommandSubcommandPrefixResult | None]) -> None:
            if t.cancelled() or t.exception() is not None:
                if memoized.cache.get(command) is lazy:  # type: ignore[attr-defined]
                    memoized.cache.delete(command)  # type: ignore[attr-defined]

        lazy = _LazyTask(
            lambda: _get_command_subcommand_prefix_impl(
                command,
                abort_signal,
                is_non_interactive_session,
                get_prefix,
                split_command,
            ),
            _on_done,
        )
        return lazy

    memoized = memoize_with_lru(
        _factory,
        lambda command, *_args: command,  # memoize by command only
        200,
    )
    return memoized


async def _query_haiku(**kwargs: Any) -> Any:
    """Command-prefix extraction via Haiku is not supported in this build.

    Invoking the extractor at runtime raises a clear ``NotImplementedError``.
    """
    raise NotImplementedError(
        "Command-prefix extraction via Haiku is not supported in this build."
    )


async def _get_command_prefix_impl(
    command: str,
    abort_signal: AbortSignal,
    is_non_interactive_session: bool,
    tool_name: str,
    policy_spec: str,
    event_name: str,
    query_source: QuerySource,
    pre_check: Callable[[str], CommandPrefixResult | None] | None,
) -> CommandPrefixResult | None:
    if os.environ.get("NODE_ENV") == "test":
        return None

    # Run pre-check if provided (e.g., isHelpCommand for Bash).
    if pre_check is not None:
        pre_check_result = pre_check(command)
        if pre_check_result is not None:
            return pre_check_result

    preflight_timer: asyncio.TimerHandle | None = None
    start_time = _now_ms()
    result: CommandPrefixResult | None = None

    def _warn_slow_preflight() -> None:
        message = (
            f"[{tool_name}Tool] Pre-flight check is taking longer than expected. "
            "Run with TABVIS_LOG=debug to check for failed or slow API requests."
        )
        if is_non_interactive_session:
            sys.stderr.write(
                json_stringify({"level": "warn", "message": message}) + "\n"
            )
        else:
            print(_chalk_yellow(f"⚠️  {message}"), file=sys.stderr)

    try:
        # Log a warning if the pre-flight check takes too long (10 seconds).
        try:
            loop = asyncio.get_running_loop()
            preflight_timer = loop.call_later(10.0, _warn_slow_preflight)
        except RuntimeError:
            preflight_timer = None

        use_system_prompt_policy_spec = False

        if use_system_prompt_policy_spec:
            system_prompt = as_system_prompt(
                [
                    f"Your task is to process {tool_name} commands that an AI coding "
                    f"agent wants to run.\n\n{policy_spec}",
                ]
            )
            user_prompt = f"Command: {command}"
        else:
            system_prompt = as_system_prompt(
                [
                    f"Your task is to process {tool_name} commands that an AI coding "
                    f"agent wants to run.\n\nThis policy spec defines how to determine "
                    f"the prefix of a {tool_name} command:",
                ]
            )
            user_prompt = f"{policy_spec}\n\nCommand: {command}"

        response = await _query_haiku(
            systemPrompt=system_prompt,
            userPrompt=user_prompt,
            signal=abort_signal,
            options={
                "enablePromptCaching": use_system_prompt_policy_spec,
                "querySource": query_source,
                "agents": [],
                "isNonInteractiveSession": is_non_interactive_session,
                "hasAppendSystemPrompt": False,
                "mcpTools": [],
            },
        )

        # Clear the timeout since the query completed.
        if preflight_timer is not None:
            preflight_timer.cancel()
        duration_ms = _now_ms() - start_time

        content = response["message"]["content"]
        if isinstance(content, str):
            prefix = content
        elif isinstance(content, list):
            text_block = next((b for b in content if b.get("type") == "text"), None)
            prefix = text_block["text"] if text_block is not None else "none"
        else:
            prefix = "none"

        if starts_with_api_error_prefix(prefix):
            result = None
        elif prefix == "command_injection_detected":
            # Haiku detected something suspicious - treat as no prefix available.
            result = {"commandPrefix": None}
        elif prefix == "git" or prefix.lower() in DANGEROUS_SHELL_PREFIXES:
            # Never accept bare `git` or shell executables as a prefix.
            result = {"commandPrefix": None}
        elif prefix == "none":
            # No prefix detected.
            result = {"commandPrefix": None}
        else:
            # Validate that the prefix is actually a prefix of the command.
            if not command.startswith(prefix):
                result = {"commandPrefix": None}
            else:
                result = {"commandPrefix": prefix}

        return result
    except Exception:
        if preflight_timer is not None:
            preflight_timer.cancel()
        raise


async def _get_command_subcommand_prefix_impl(
    command: str,
    abort_signal: AbortSignal,
    is_non_interactive_session: bool,
    get_prefix: Callable[[str, AbortSignal, bool], Awaitable[CommandPrefixResult | None]],
    split_command_fn: Callable[[str], list[str] | Awaitable[list[str]]],
) -> CommandSubcommandPrefixResult | None:
    split_result = split_command_fn(command)
    subcommands = await split_result if asyncio.iscoroutine(split_result) else split_result

    async def _sub(subcommand: str) -> dict[str, Any]:
        return {
            "subcommand": subcommand,
            "prefix": await get_prefix(
                subcommand, abort_signal, is_non_interactive_session
            ),
        }

    gathered = await asyncio.gather(
        get_prefix(command, abort_signal, is_non_interactive_session),
        *[_sub(subcommand) for subcommand in subcommands],
    )
    full_command_prefix = gathered[0]
    subcommand_prefixes_results = gathered[1:]

    if not full_command_prefix:
        return None

    subcommand_prefixes: dict[str, CommandPrefixResult] = {}
    for entry in subcommand_prefixes_results:
        prefix = entry["prefix"]
        if prefix:
            subcommand_prefixes[entry["subcommand"]] = prefix

    return {**full_command_prefix, "subcommandPrefixes": subcommand_prefixes}
