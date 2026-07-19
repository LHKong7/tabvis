"""Session-start / setup hook orchestration

Two public coroutines drive the SessionStart / Setup hook lifecycle and fold their
results into a flat list of transcript ``HookResultMessage`` envelopes:

* :func:`process_session_start_hooks` — runs the ``SessionStart`` hooks for a given
  ``source`` (``startup`` / ``resume`` / ``clear`` / ``compact``), collecting hook
  messages, additional-context attachments and watch paths.
* :func:`process_setup_hooks` — the analogous driver for ``Setup`` hooks
  (``init`` / ``maintenance``).

Plus the tiny side-channel :func:`take_initial_user_message`, which lets a hook smuggle
an ``initialUserMessage`` to print-mode without widening the coroutine return type
(faithful to the TS comment on the same module-level latch).

Casing: Python identifiers are snake_case; the attachment/hook-message dicts that
round-trip to the transcript keep their camelCase wire keys (``hookName``, ``toolUseID``,
``hookEvent``) verbatim.

CYCLE NOTE: ``attachments`` and ``hooks`` are mutually-recursive cycle siblings of this
module (they are implemented in parallel and may not exist on disk yet). EVERY reference to a
cycle sibling is therefore broken with a function-local (lazy) import so this module
imports STANDALONE. ``hooks.executeSessionStartHooks`` / ``executeSetupHooks`` are not
implemented in this build; the lazy import is guarded so a clean env (no configured hooks)
yields no messages — observably identical to the ``--bare`` / no-hooks path.

stdlib substitutions: ``async function*`` consumers (``for await ... of``) -> ``async for``;
no third-party deps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from tabvis.types.message import HookResultMessage

# --- side channel (faithful to the TS module-level latch) -------------------
#
# Set by :func:`process_session_start_hooks` when a hook emits ``initialUserMessage``;
# consumed once by :func:`take_initial_user_message`. This side channel avoids changing
# the ``list[HookResultMessage]`` return type that ``main`` and ``print`` both already
# await on — rippling a structural return-type change through that handoff would touch
# five callsites for what is a print-mode-only value.
_pending_initial_user_message: str | None = None


def take_initial_user_message() -> str | None:
    """Return and clear the pending initial-user-message side channel (consumed once)."""
    global _pending_initial_user_message
    v = _pending_initial_user_message
    _pending_initial_user_message = None
    return v


# Note: do NOT add ANY "warmup" logic. It is **CRITICAL** that no extra work is added on
# startup.
async def process_session_start_hooks(
    source: Literal["startup", "resume", "clear", "compact"],
    *,
    session_id: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    force_sync_execution: bool | None = None,
) -> list[HookResultMessage]:
    """Run ``SessionStart`` hooks for ``source`` and fold their results into messages.

    ``--bare`` skips all hooks (the executor early-returns under ``--bare`` too, but
    returning here avoids extra setup work). With no configured hooks the executor yields
    nothing — an empty list, identical to the bare path.
    """
    global _pending_initial_user_message

    # Lazy import: env_utils' bare-mode flag lives in tabvis.agent.setup (per the implementation ledger).
    from tabvis.agent.setup import is_bare_mode

    if is_bare_mode():
        return []

    hook_messages: list[HookResultMessage] = []
    additional_contexts: list[str] = []
    all_watch_paths: list[str] = []

    # Use the provided agent_type or fall back to the one stored in bootstrap state.
    from tabvis.bootstrap.state import get_main_thread_agent_type

    resolved_agent_type = (
        agent_type if agent_type is not None else get_main_thread_agent_type()
    )

    async for hook_result in _execute_session_start_hooks(
        source,
        session_id,
        resolved_agent_type,
        model,
        force_sync_execution,
    ):
        message = hook_result.get("message")
        if message:
            hook_messages.append(message)
        contexts = hook_result.get("additionalContexts")
        if contexts:
            additional_contexts.extend(contexts)
        initial = hook_result.get("initialUserMessage")
        if initial:
            _pending_initial_user_message = initial
        watch_paths = hook_result.get("watchPaths")
        if watch_paths:
            all_watch_paths.extend(watch_paths)

    if all_watch_paths:
        # Lazy import (cycle sibling: hooks/fileChangedWatcher).
        from tabvis.utils.hooks.file_changed_watcher import update_watch_paths

        update_watch_paths(all_watch_paths)

    # If hooks provided additional context, add it as a message.
    if additional_contexts:
        # Lazy import (cycle sibling: attachments).
        from tabvis.utils.attachments import create_attachment_message

        context_message = create_attachment_message(
            {
                "type": "hook_additional_context",
                "content": additional_contexts,
                "hookName": "SessionStart",
                "toolUseID": "SessionStart",
                "hookEvent": "SessionStart",
            }
        )
        hook_messages.append(context_message)

    return hook_messages


async def process_setup_hooks(
    trigger: Literal["init", "maintenance"],
    *,
    force_sync_execution: bool | None = None,
) -> list[HookResultMessage]:
    """Run ``Setup`` hooks for ``trigger`` and fold their results into messages."""
    from tabvis.agent.setup import is_bare_mode

    if is_bare_mode():
        return []

    hook_messages: list[HookResultMessage] = []
    additional_contexts: list[str] = []

    async for hook_result in _execute_setup_hooks(trigger, force_sync_execution):
        message = hook_result.get("message")
        if message:
            hook_messages.append(message)
        contexts = hook_result.get("additionalContexts")
        if contexts:
            additional_contexts.extend(contexts)

    if additional_contexts:
        from tabvis.utils.attachments import create_attachment_message

        context_message = create_attachment_message(
            {
                "type": "hook_additional_context",
                "content": additional_contexts,
                "hookName": "Setup",
                "toolUseID": "Setup",
                "hookEvent": "Setup",
            }
        )
        hook_messages.append(context_message)

    return hook_messages


# --- lazy hook-executor adapters --------------------------------------------
#
# :mod:`tabvis.utils.hooks` (SessionStart/Setup events are not wired yet). These thin async
# generators import the executor lazily (cycle sibling) and, when it is absent, yield
# nothing — which is the same observable result as a clean env with no configured hooks.


async def _execute_session_start_hooks(
    source: str,
    session_id: str | None,
    agent_type: str | None,
    model: str | None,
    force_sync_execution: bool | None,
):
    try:
        from tabvis.utils.hooks import (  # type: ignore[attr-defined]
            execute_session_start_hooks,
        )
    except ImportError:
        return
    async for result in execute_session_start_hooks(
        source,
        session_id,
        agent_type,
        model,
        None,
        None,
        force_sync_execution,
    ):
        yield result


async def _execute_setup_hooks(
    trigger: str,
    force_sync_execution: bool | None,
):
    try:
        from tabvis.utils.hooks import execute_setup_hooks  # type: ignore[attr-defined]
    except ImportError:
        return
    async for result in execute_setup_hooks(
        trigger,
        None,
        None,
        force_sync_execution,
    ):
        yield result
