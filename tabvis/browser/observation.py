"""Observation Normalizer + Timeline (OBS-4).

The Normalizer maps the raw ``action.performed`` events a producer puts on the bus (OBS-3) into the
Semantic Observations the design wants an agent to consume — ``navigation.performed`` /
``element.interacted`` / ``search.performed`` / ``page.loaded`` (``design.md`` §3 / §"Observation
Normalizer"). Each observation is appended to a per-agent **Timeline** and re-published onto the bus
so downstream sinks (the SSE observation stream, OBS-5) can forward it.

:func:`install_observation_pipeline` wires the normalizer as a bus sink, once. It is safe to call
eagerly — the bus is no-op unless ``TABVIS_BROWSER_EVENT_BUS`` is on, so with the flag off nothing is
normalized, appended, or forwarded.
"""

from __future__ import annotations

import os
from typing import Any

from tabvis.browser.event_bus import get_event_bus
from tabvis.browser.events import ObservationType, RawEventType, RuntimeEvent
from tabvis.utils.debug import log_for_debugging

# The semantic observation types a forwarding sink should surface (vs raw producer events).
OBSERVATION_TYPES: frozenset[str] = frozenset(
    {
        ObservationType.PAGE_LOADED,
        ObservationType.NAVIGATION_PERFORMED,
        ObservationType.ELEMENT_INTERACTED,
        ObservationType.SEARCH_PERFORMED,
        ObservationType.ARTIFACT_DOWNLOADED,
        ObservationType.DIALOG_OPENED,
        ObservationType.CONSOLE_ERROR,
    }
)

_MAX_TIMELINE = 500
_timeline: dict[str, list[dict[str, Any]]] = {}


def normalize(raw: RuntimeEvent) -> RuntimeEvent | None:
    """Map a raw ``action.performed`` event to a Semantic Observation. None for anything else.

    Returning None for non-raw (already-semantic) events is what keeps the re-publish loop finite:
    the normalizer sink is subscribed to every event, but only raw producer events are transformed.
    """
    if raw.type != RawEventType.ACTION_PERFORMED:
        return None
    payload = raw.payload or {}
    event_type = payload.get("event_type")
    if payload.get("intent") == "search":
        obs_type = ObservationType.SEARCH_PERFORMED
    elif event_type == "navigation":
        obs_type = ObservationType.NAVIGATION_PERFORMED
    elif event_type == "interaction":
        obs_type = ObservationType.ELEMENT_INTERACTED
    else:
        obs_type = ObservationType.PAGE_LOADED
    body = {
        "action": payload.get("action"),
        "url": payload.get("url"),
        "title": payload.get("title"),
        "tab_count": payload.get("tab_count"),
    }
    return RuntimeEvent(
        type=obs_type,
        source=raw.source,
        payload=body,
        agent_id=raw.agent_id,
        workspace_id=raw.workspace_id,
        session_id=raw.session_id,
        execution_id=raw.execution_id,
    )


def _append_timeline(obs: RuntimeEvent) -> None:
    key = obs.agent_id or "default"
    entries = _timeline.setdefault(key, [])
    entries.append({"type": obs.type, "at": obs.timestamp, **(obs.payload or {})})
    if len(entries) > _MAX_TIMELINE:
        del entries[: len(entries) - _MAX_TIMELINE]


def get_timeline(agent_id: str | None) -> list[dict[str, Any]]:
    """The semantic-observation timeline for an agent (oldest first). Feeds WS-5's Workspace timeline."""
    return list(_timeline.get(agent_id or "default", []))


def clear_timeline(agent_id: str | None = None) -> None:
    if agent_id is None:
        _timeline.clear()
    else:
        _timeline.pop(agent_id or "default", None)


async def _normalizer_sink(raw: RuntimeEvent) -> None:
    obs = normalize(raw)
    if obs is None:
        return
    _append_timeline(obs)
    # Re-publish the semantic observation so forwarding sinks (OBS-5) receive it. The normalizer
    # ignores non-raw events, so this does not loop.
    await get_event_bus().publish(obs)


_installed = False


def install_observation_pipeline() -> None:
    """Subscribe the normalizer to the bus, once. Safe to call eagerly (bus is gated)."""
    global _installed
    if _installed:
        return
    _installed = True
    get_event_bus().subscribe(_normalizer_sink)


async def emit_observation(event: RuntimeEvent) -> None:
    """Record an already-semantic observation (e.g. from an OBS-6 adapter): append it to the timeline
    and publish it for forwarding sinks. The normalizer ignores non-raw events, so this never loops.
    """
    _append_timeline(event)
    await get_event_bus().publish(event)


def is_replay_enabled() -> bool:
    """Whether raw data is declared for replay retention (OBS-7). ``TABVIS_BROWSER_REPLAY`` (default OFF).

    Off (the default) means raw producer events stay ephemeral — only the artifact log (opt-out) and
    the in-memory timeline persist, matching the design's "Raw Event 默认不长期保存" retention.
    """
    val = os.environ.get("TABVIS_BROWSER_REPLAY")
    return bool(val) and val.strip().lower() not in ("0", "false", "no", "off", "")


def persist_timeline(agent_id: str | None, session_id: str | None = None) -> str | None:
    """Persist an agent's semantic timeline as a ``replay.json`` (OBS-7), only when replay is declared.

    This is the "declared for replay" retention path — the reconstructable record a replay needs.
    Returns the file path, or None if replay is off or the timeline is empty.
    """
    if not is_replay_enabled():
        return None
    entries = get_timeline(agent_id)
    if not entries:
        return None
    try:
        import json

        from tabvis.browser.artifacts import get_artifacts_dir

        directory = get_artifacts_dir(session_id)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "replay.json")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, default=str)
        os.replace(tmp, path)
        return path
    except Exception as e:  # noqa: BLE001
        log_for_debugging(f"[OBSERVATION] failed to persist replay: {e}")
        return None
