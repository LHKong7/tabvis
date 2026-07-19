"""Hook settings aggregation + display helpers

Collects the individual hook configs across editable settings sources (user / project / local) plus
in-memory session hooks, groups/sorts them for the ``/hooks`` UI, and renders source display strings.

Bounded: ``get_session_hooks`` (``src/utils/hooks/sessionHooks.ts``) is not implemented, so the
session-hook merge is lazily attempted and degrades to "no session hooks" when the module is
absent. ``DEFAULT_HOOK_SHELL`` is reused from the existing shell provider.

Casing: Python identifiers snake_case; the settings/source wire keys (``userSettings`` etc.) and
the hook-config dict keys (``command``/``prompt``/``url``/``matcher``/``if``) stay verbatim.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from tabvis.utils.settings.constants import (
    EditableSettingSource,  # noqa: F401 - re-exported type alias for parity
    get_settings_file_path_for_source,
)
from tabvis.utils.settings.settings import get_settings_for_source
from tabvis.utils.shell.shell_provider import DEFAULT_HOOK_SHELL

# HookSource = EditableSettingSource | 'policySettings' | 'sessionHook' | 'builtinHook'.
HookSource = str

# A HookCommand is a loose dict (the discriminated union of command/prompt/agent/http/callback/
# function hooks). An IndividualHookConfig is {event, config, matcher?, source}.
HookCommand = dict[str, Any]
IndividualHookConfig = dict[str, Any]

# SOURCES priority order (low index = higher priority). Mirrors ``settings/constants.SOURCES``
# (the editable sources only) used by ``sortMatchersByPriority``.
SOURCES: tuple[EditableSettingSource, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
)


def is_hook_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two hooks are equal — comparing command/prompt content (+ shell + ``if``), not timeout.

    ``If`` is part of identity (same command with different ``if``
    conditions are distinct hooks). For ``command`` hooks, ``shell`` is part of identity with the
    ``DEFAULT_HOOK_SHELL`` default. ``function`` hooks have no stable identifier -> never equal.
    """
    if a.get("type") != b.get("type"):
        return False

    def same_if(x: dict[str, Any], y: dict[str, Any]) -> bool:
        return (x.get("if") or "") == (y.get("if") or "")

    kind = a.get("type")
    if kind == "command":
        return (
            b.get("type") == "command"
            and a.get("command") == b.get("command")
            and (a.get("shell") or DEFAULT_HOOK_SHELL) == (b.get("shell") or DEFAULT_HOOK_SHELL)
            and same_if(a, b)
        )
    if kind == "prompt":
        return b.get("type") == "prompt" and a.get("prompt") == b.get("prompt") and same_if(a, b)
    if kind == "agent":
        return b.get("type") == "agent" and a.get("prompt") == b.get("prompt") and same_if(a, b)
    if kind == "http":
        return b.get("type") == "http" and a.get("url") == b.get("url") and same_if(a, b)
    # 'function' (and unknown types) — no stable identifier.
    return False


def get_hook_display_text(hook: dict[str, Any]) -> str:
    """Get the display text for a hook.

    A custom ``statusMessage`` takes precedence; otherwise the per-type field (command/prompt/url)
    or the literal ``'callback'`` / ``'function'``.
    """
    status_message = hook.get("statusMessage")
    if status_message:
        return status_message

    kind = hook.get("type")
    if kind == "command":
        return hook.get("command", "")
    if kind in ("prompt", "agent"):
        return hook.get("prompt", "")
    if kind == "http":
        return hook.get("url", "")
    if kind == "callback":
        return "callback"
    if kind == "function":
        return "function"
    return ""


def _get_session_hooks(app_state: Any, session_id: str) -> dict[str, list[dict[str, Any]]]:
    """Lazily fetch session hooks; empty map when session hooks are unavailable."""
    try:
        from tabvis.utils.hooks.session_hooks import get_session_hooks  # type: ignore[attr-defined]
    except ImportError:
        return {}
    try:
        return dict(get_session_hooks(app_state, session_id))
    except Exception:  # noqa: BLE001 - degrade to no session hooks
        return {}


def get_all_hooks(app_state: Any) -> list[IndividualHookConfig]:
    """Collect every individual hook config across sources + session hooks.

    Reads the editable settings sources (user / project / local), de-duplicating by resolved file
    path (so a home-dir run where user and project settings collapse to the same file is not
    double-counted), then appends in-memory session hooks.

    ``allowManagedHooksOnly`` policy gating is read from the ``policySettings`` source; when unset,
    ``restricted_to_managed_only`` is False and all editable sources are collected.
    """
    hooks: list[IndividualHookConfig] = []

    policy_settings = get_settings_for_source("policySettings")
    restricted_to_managed_only = policy_settings.get("allowManagedHooksOnly") is True

    if not restricted_to_managed_only:
        sources: tuple[EditableSettingSource, ...] = (
            "userSettings",
            "projectSettings",
            "localSettings",
        )
        seen_files: set[str] = set()

        for source in sources:
            file_path = get_settings_file_path_for_source(source)
            if file_path:
                resolved_path = os.path.abspath(file_path)
                if resolved_path in seen_files:
                    continue
                seen_files.add(resolved_path)

            source_settings = get_settings_for_source(source)
            source_hooks = source_settings.get("hooks")
            if not source_hooks:
                continue

            for event, matchers in source_hooks.items():
                for matcher in matchers:
                    for hook_command in matcher.get("hooks", []):
                        hooks.append(
                            {
                                "event": event,
                                "config": hook_command,
                                "matcher": matcher.get("matcher"),
                                "source": source,
                            }
                        )

    # Session hooks.
    from tabvis.agent.api.client import get_session_id

    session_id = get_session_id()
    session_hooks = _get_session_hooks(app_state, session_id)
    for event, matchers in session_hooks.items():
        for matcher in matchers:
            for hook_command in matcher.get("hooks", []):
                hooks.append(
                    {
                        "event": event,
                        "config": hook_command,
                        "matcher": matcher.get("matcher"),
                        "source": "sessionHook",
                    }
                )

    return hooks


def get_hooks_for_event(app_state: Any, event: str) -> list[IndividualHookConfig]:
    """The hooks whose ``event`` matches."""
    return [hook for hook in get_all_hooks(app_state) if hook["event"] == event]


def hook_source_description_display_string(source: HookSource) -> str:
    """Return a sentence describing the hook source."""
    return {
        "userSettings": "User settings (~/.tabvis/settings.json)",
        "projectSettings": "Project settings (.tabvis/settings.json)",
        "localSettings": "Local settings (.tabvis/settings.local.json)",
        "sessionHook": "Session hooks (in-memory, temporary)",
        "builtinHook": "Built-in hooks (registered internally by Tabvis)",
    }.get(source, source)


def hook_source_header_display_string(source: HookSource) -> str:
    """Return the hook source label used in section headers."""
    return {
        "userSettings": "User Settings",
        "projectSettings": "Project Settings",
        "localSettings": "Local Settings",
        "sessionHook": "Session Hooks",
        "builtinHook": "Built-in Hooks",
    }.get(source, source)


def hook_source_inline_display_string(source: HookSource) -> str:
    """Return the compact inline label for a hook source."""
    return {
        "userSettings": "User",
        "projectSettings": "Project",
        "localSettings": "Local",
        "sessionHook": "Session",
        "builtinHook": "Built-in",
    }.get(source, source)


def sort_matchers_by_priority(
    matchers: list[str],
    hooks_by_event_and_matcher: dict[str, dict[str, list[IndividualHookConfig]]],
    selected_event: str,
) -> list[str]:
    """Sort matcher keys by their highest-priority source, then by name.

    Source priority follows :data:`SOURCES` (lower index = higher priority); ``builtinHook`` sorts
    last (priority 999). A stable, name-based tiebreak (``localeCompare`` -> Python string compare)
    breaks equal-priority ties.
    """
    source_priority: dict[str, int] = {source: index for index, source in enumerate(SOURCES)}

    def get_source_priority(source: HookSource) -> int:
        return 999 if source == "builtinHook" else source_priority.get(source, 999)

    def highest_priority(matcher: str) -> int:
        event_hooks = hooks_by_event_and_matcher.get(selected_event, {}).get(matcher, [])
        # Dedup-preserving-order on sources, then take the minimum priority.
        seen: list[str] = []
        for hook in event_hooks:
            src = hook.get("source")
            if src not in seen:
                seen.append(src)
        if not seen:
            return min(get_source_priority(s) for s in SOURCES) if SOURCES else 999
        return min(get_source_priority(s) for s in seen)

    # `sorted` with a (priority, name) key reproduces the TS comparator
    # (priority difference first, then localeCompare) and is stable.
    return sorted(matchers, key=lambda m: (highest_priority(m), m))


# A literal alias kept for symmetry with the TS ``HookSource`` union members.
_HOOK_SOURCE_LITERAL = Literal[
    "userSettings",
    "projectSettings",
    "localSettings",
    "policySettings",
    "sessionHook",
    "builtinHook",
]
