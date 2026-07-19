"""Session-scoped hook registry

Session hooks are temporary, in-memory per-session runtime callbacks (command/prompt hooks plus
in-process ``function`` hooks). They live on ``AppState.sessionHooks`` — a ``dict[str, SessionStore]``
(the TS ``Map``) keyed by session id. The store is mutated in place and the same ``prev`` object is
returned from the ``set_app_state`` updater so ``store.py``'s ``Object.is(next, prev)`` short-circuit
skips listener notification (session hooks are never reactively read — only snapshotted in the query
loop).

``HookCommand``/``HooksSettings`` are dict aliases (``tabvis.utils.settings.types``). ``HookEvent``/
``HOOK_EVENTS`` come from the hooks-core SDK types being implemented in parallel — imported lazily to keep
this module import-clean standalone.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from tabvis.utils.debug import log_for_debugging

# Type aliases (dict-shaped, wire keys verbatim).
# A command/prompt/agent/http hook config: dict with a "type" discriminator.
HookCommand = dict[str, Any]
# A function hook: {"type": "function", "id"?, "timeout"?, "callback", "errorMessage", "statusMessage"?}.
FunctionHook = dict[str, Any]

# A function hook callback: (messages, signal?) -> bool | Awaitable[bool].
# True = check passes, False = block. Stored verbatim on the function-hook dict.
FunctionHookCallback = Callable[..., Any]

# onHookSuccess: (hook, result) -> None.
OnHookSuccess = Callable[[HookCommand | FunctionHook, Any], None]

# A session hook matcher: {"matcher": str, "skillRoot"?: str, "hooks": [{"hook", "onHookSuccess"?}]}.
SessionHookMatcher = dict[str, Any]

# A SessionStore: {"hooks": {event: [SessionHookMatcher, ...]}}.
SessionStore = dict[str, Any]

# SessionHooksState: dict[str, SessionStore] (the TS Map keyed by session id).
SessionHooksState = dict[str, "SessionStore"]

# A regular (persistable) hook matcher derived from session matchers, skillRoot preserved.
SessionDerivedHookMatcher = dict[str, Any]


def _hook_events() -> tuple[str, ...]:
    """Lazily import ``HOOK_EVENTS`` (hooks-core sibling implemented in parallel)."""
    from tabvis.utils.hooks.hook_events import HOOK_EVENTS  # noqa: PLC0415

    return tuple(HOOK_EVENTS)


def _session_hooks_map(app_state: Any) -> SessionHooksState:
    """Return the ``sessionHooks`` map off the AppState (dict-shaped, camelCase wire key).

    The implemented ``AppState`` is a ``TypedDict`` (plain dict) keyed by camelCase wire keys
    (``mainLoopModel`` etc.), per the spine contract. ``sessionHooks`` is the in-memory session-hook
    map (the TS ``Map``); it may not be pre-initialized in the AppState slice implemented in parallel, so
    we materialize it on first access. Attribute-style access (``app_state.sessionHooks``) is also
    tolerated for forward-compat with an object-shaped AppState.
    """
    if isinstance(app_state, dict):
        existing = app_state.get("sessionHooks")
        if existing is None:
            existing = {}
            app_state["sessionHooks"] = existing
        return existing
    existing = getattr(app_state, "sessionHooks", None)
    if existing is None:
        existing = {}
        try:
            app_state.sessionHooks = existing
        except (AttributeError, TypeError):  # pragma: no cover - read-only forward-compat
            pass
    return existing


def _is_hook_equal(
    a: HookCommand | FunctionHook, b: HookCommand | FunctionHook
) -> bool:
    """Lazily delegate to ``hooksSettings.isHookEqual`` (hooks-core sibling)."""
    from tabvis.utils.hooks.hooks_settings import is_hook_equal  # noqa: PLC0415

    return is_hook_equal(a, b)


def add_session_hook(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    event: str,
    matcher: str,
    hook: HookCommand,
    on_hook_success: OnHookSuccess | None = None,
    skill_root: str | None = None,
) -> None:
    """Add a command or prompt hook to the session.

    Session hooks are temporary, in-memory only, and cleared when the session ends.
    """
    _add_hook_to_session(
        set_app_state,
        session_id,
        event,
        matcher,
        hook,
        on_hook_success,
        skill_root,
    )


def add_function_hook(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    event: str,
    matcher: str,
    callback: FunctionHookCallback,
    error_message: str,
    options: dict[str, Any] | None = None,
) -> str:
    """Add a function hook to the session.

    Function hooks execute in-process callbacks for validation.

    :returns: The hook ID (for removal).
    """
    options = options or {}
    hook_id = options.get("id") or f"function-hook-{int(time.time() * 1000)}-{random.random()}"
    hook: FunctionHook = {
        "type": "function",
        "id": hook_id,
        "timeout": options.get("timeout") or 5000,
        "callback": callback,
        "errorMessage": error_message,
    }
    _add_hook_to_session(set_app_state, session_id, event, matcher, hook)
    return hook_id


def remove_function_hook(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    event: str,
    hook_id: str,
) -> None:
    """Remove a function hook by ID from the session."""

    def _updater(prev: Any) -> Any:
        session_hooks = _session_hooks_map(prev)
        store = session_hooks.get(session_id)
        if not store:
            return prev

        event_matchers = store["hooks"].get(event) or []

        # Remove the hook with matching ID from all matchers.
        updated_matchers: list[SessionHookMatcher] = []
        for matcher in event_matchers:
            updated_hooks = [
                h
                for h in matcher["hooks"]
                if h["hook"].get("type") != "function" or h["hook"].get("id") != hook_id
            ]
            if len(updated_hooks) > 0:
                updated_matchers.append({**matcher, "hooks": updated_hooks})

        if len(updated_matchers) > 0:
            new_hooks = {**store["hooks"], event: updated_matchers}
        else:
            new_hooks = {e: v for e, v in store["hooks"].items() if e != event}

        session_hooks[session_id] = {"hooks": new_hooks}
        return prev

    set_app_state(_updater)

    log_for_debugging(
        f"Removed function hook {hook_id} for event {event} in session {session_id}"
    )


def _add_hook_to_session(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    event: str,
    matcher: str,
    hook: HookCommand | FunctionHook,
    on_hook_success: OnHookSuccess | None = None,
    skill_root: str | None = None,
) -> None:
    """Internal helper to add a hook to session state."""

    def _updater(prev: Any) -> Any:
        session_hooks = _session_hooks_map(prev)
        store = session_hooks.get(session_id) or {"hooks": {}}
        event_matchers = store["hooks"].get(event) or []

        # Find existing matcher or create new one.
        existing_index = -1
        for i, m in enumerate(event_matchers):
            if m["matcher"] == matcher and m.get("skillRoot") == skill_root:
                existing_index = i
                break

        if existing_index >= 0:
            # Add to existing matcher.
            updated_matchers = list(event_matchers)
            existing_matcher = updated_matchers[existing_index]
            updated_matchers[existing_index] = {
                "matcher": existing_matcher["matcher"],
                "skillRoot": existing_matcher.get("skillRoot"),
                "hooks": [
                    *existing_matcher["hooks"],
                    {"hook": hook, "onHookSuccess": on_hook_success},
                ],
            }
        else:
            # Create new matcher.
            updated_matchers = [
                *event_matchers,
                {
                    "matcher": matcher,
                    "skillRoot": skill_root,
                    "hooks": [{"hook": hook, "onHookSuccess": on_hook_success}],
                },
            ]

        new_hooks = {**store["hooks"], event: updated_matchers}

        session_hooks[session_id] = {"hooks": new_hooks}
        return prev

    set_app_state(_updater)

    log_for_debugging(f"Added session hook for event {event} in session {session_id}")


def remove_session_hook(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    event: str,
    hook: HookCommand,
) -> None:
    """Remove a specific hook from the session.

    :param set_app_state: The function to update the app state.
    :param session_id: The session ID.
    :param event: The hook event.
    :param hook: The hook command to remove.
    """

    def _updater(prev: Any) -> Any:
        session_hooks = _session_hooks_map(prev)
        store = session_hooks.get(session_id)
        if not store:
            return prev

        event_matchers = store["hooks"].get(event) or []

        # Remove the hook from all matchers.
        updated_matchers: list[SessionHookMatcher] = []
        for matcher in event_matchers:
            updated_hooks = [
                h for h in matcher["hooks"] if not _is_hook_equal(h["hook"], hook)
            ]
            if len(updated_hooks) > 0:
                updated_matchers.append({**matcher, "hooks": updated_hooks})

        if len(updated_matchers) > 0:
            new_hooks = {**store["hooks"], event: updated_matchers}
        else:
            new_hooks = {**store["hooks"]}

        if len(updated_matchers) == 0:
            new_hooks.pop(event, None)

        session_hooks[session_id] = {**store, "hooks": new_hooks}
        return prev

    set_app_state(_updater)

    log_for_debugging(f"Removed session hook for event {event} in session {session_id}")


def _convert_to_hook_matchers(
    session_matchers: list[SessionHookMatcher],
) -> list[SessionDerivedHookMatcher]:
    """Convert session hook matchers to regular hook matchers.

    Function hooks are filtered out — they can't be persisted to HookMatcher format. The optional
    ``skillRoot`` is preserved.
    """
    return [
        {
            "matcher": sm["matcher"],
            "skillRoot": sm.get("skillRoot"),
            "hooks": [
                h["hook"] for h in sm["hooks"] if h["hook"].get("type") != "function"
            ],
        }
        for sm in session_matchers
    ]


def get_session_hooks(
    app_state: Any,
    session_id: str,
    event: str | None = None,
) -> dict[str, list[SessionDerivedHookMatcher]]:
    """Get all session hooks for a specific event (excluding function hooks).

    :returns: Hook matchers for the event keyed by event, or all hooks if no event specified.
    """
    store = _session_hooks_map(app_state).get(session_id)
    if not store:
        return {}

    result: dict[str, list[SessionDerivedHookMatcher]] = {}

    if event:
        session_matchers = store["hooks"].get(event)
        if session_matchers:
            result[event] = _convert_to_hook_matchers(session_matchers)
        return result

    for evt in _hook_events():
        session_matchers = store["hooks"].get(evt)
        if session_matchers:
            result[evt] = _convert_to_hook_matchers(session_matchers)

    return result


def get_session_function_hooks(
    app_state: Any,
    session_id: str,
    event: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Get all session function hooks for a specific event.

    Function hooks are kept separate because they can't be persisted to HookMatcher format.

    :returns: Function hook matchers (``{"matcher", "hooks": [FunctionHook, ...]}``) keyed by event.
    """
    store = _session_hooks_map(app_state).get(session_id)
    if not store:
        return {}

    result: dict[str, list[dict[str, Any]]] = {}

    def extract_function_hooks(
        session_matchers: list[SessionHookMatcher],
    ) -> list[dict[str, Any]]:
        out = []
        for sm in session_matchers:
            fhooks = [
                h["hook"] for h in sm["hooks"] if h["hook"].get("type") == "function"
            ]
            m = {"matcher": sm["matcher"], "hooks": fhooks}
            if len(m["hooks"]) > 0:
                out.append(m)
        return out

    if event:
        session_matchers = store["hooks"].get(event)
        if session_matchers:
            function_matchers = extract_function_hooks(session_matchers)
            if len(function_matchers) > 0:
                result[event] = function_matchers
        return result

    for evt in _hook_events():
        session_matchers = store["hooks"].get(evt)
        if session_matchers:
            function_matchers = extract_function_hooks(session_matchers)
            if len(function_matchers) > 0:
                result[evt] = function_matchers

    return result


def get_session_hook_callback(
    app_state: Any,
    session_id: str,
    event: str,
    matcher: str,
    hook: HookCommand | FunctionHook,
) -> dict[str, Any] | None:
    """Get the full hook entry (including callbacks) for a specific session hook."""
    store = _session_hooks_map(app_state).get(session_id)
    if not store:
        return None

    event_matchers = store["hooks"].get(event)
    if not event_matchers:
        return None

    # Find the hook in the matchers.
    for matcher_entry in event_matchers:
        if matcher_entry["matcher"] == matcher or matcher == "":
            for hook_entry in matcher_entry["hooks"]:
                if _is_hook_equal(hook_entry["hook"], hook):
                    return hook_entry

    return None


def clear_session_hooks(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
) -> None:
    """Clear all session hooks for a specific session.

    :param set_app_state: The function to update the app state.
    :param session_id: The session ID.
    """

    def _updater(prev: Any) -> Any:
        _session_hooks_map(prev).pop(session_id, None)
        return prev

    set_app_state(_updater)

    log_for_debugging(f"Cleared all session hooks for session {session_id}")
