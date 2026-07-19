"""Register a skill's frontmatter hooks as session hooks.

Hooks are registered as session-scoped hooks that persist for the duration of the session. If a hook
has ``once: true``, an ``on_hook_success`` callback removes it after its first successful execution.

``HOOK_EVENTS`` is a hooks-core sibling implemented in parallel — imported lazily.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tabvis.utils.debug import log_for_debugging
from tabvis.utils.hooks.session_hooks import add_session_hook, remove_session_hook

# HooksSettings: event name -> list of matchers (dict-shaped, wire keys verbatim).
HooksSettings = dict[str, list[dict[str, Any]]]


def register_skill_hooks(
    set_app_state: Callable[[Callable[[Any], Any]], None],
    session_id: str,
    hooks: HooksSettings,
    skill_name: str,
    skill_root: str | None = None,
) -> None:
    """Register hooks from a skill's frontmatter as session hooks.

    :param set_app_state: Function to update the app state.
    :param session_id: The current session ID.
    :param hooks: The hooks settings from the skill's frontmatter.
    :param skill_name: The name of the skill (for logging).
    :param skill_root: The base directory of the skill (for the ``TABVIS_SKILL_ROOT`` env var).
    """
    from tabvis.utils.hooks.hook_events import HOOK_EVENTS  # noqa: PLC0415

    registered_count = 0

    for event_name in HOOK_EVENTS:
        matchers = hooks.get(event_name)
        if not matchers:
            continue

        for matcher in matchers:
            for hook in matcher["hooks"]:
                # For once: true hooks, use on_hook_success callback to remove after execution.
                on_hook_success: Callable[..., None] | None
                if hook.get("once"):

                    def _on_success(
                        _h: Any,
                        _r: Any,
                        _event_name: str = event_name,
                        _hook: Any = hook,
                    ) -> None:
                        log_for_debugging(
                            f"Removing one-shot hook for event {_event_name} in skill '{skill_name}'"
                        )
                        remove_session_hook(set_app_state, session_id, _event_name, _hook)

                    on_hook_success = _on_success
                else:
                    on_hook_success = None

                add_session_hook(
                    set_app_state,
                    session_id,
                    event_name,
                    matcher.get("matcher") or "",
                    hook,
                    on_hook_success,
                    skill_root,
                )
                registered_count += 1

    if registered_count > 0:
        log_for_debugging(f"Registered {registered_count} hooks from skill '{skill_name}'")
