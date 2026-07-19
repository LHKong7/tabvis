"""Playwright observation producers (OBS-6).

``design.md`` §"Observation Normalizer" lists more sources than the DOM snapshot: downloads, dialogs,
console, network. This attaches best-effort Playwright page listeners that publish Semantic
Observations onto the EventBus: ``download`` → ``artifact.downloaded`` and a console *error* →
``console.error``. It is gated by ``TABVIS_BROWSER_EVENT_BUS`` and every listener is fully guarded — a
producer error can never fail a browser action (the same discipline as ``observe()`` / the artifact
trail).

Kept deliberately narrow for safety: dialogs are NOT intercepted (a mishandled dialog listener can
hang a page — Playwright's default auto-dismiss is left in place), and the deeper CDP Network adapter
is a documented follow-up. Downloads and console errors are low-risk, cross-engine signals.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tabvis.browser.event_bus import is_event_bus_enabled
from tabvis.browser.events import ObservationType, RuntimeEvent
from tabvis.utils.debug import log_for_debugging

# Retain in-flight emit tasks so the event loop keeps a strong reference (a bare ensure_future task
# is only weakly held and can be garbage-collected before it runs, silently dropping the event).
_pending_tasks: set[asyncio.Task[Any]] = set()


def build_observation(
    obs_type: str, payload: dict[str, Any], *, agent_id: str | None, session_id: str | None
) -> RuntimeEvent:
    """Construct a Playwright-sourced Semantic Observation (pure; unit-testable without a browser)."""
    return RuntimeEvent(
        type=obs_type, source="playwright", payload=payload, agent_id=agent_id, session_id=session_id
    )


def attach_page_producers(
    page: Any, *, agent_id: str | None = None, session_id: str | None = None
) -> None:
    """Attach download / console-error producers to a page (OBS-6). No-op unless the bus is on.

    Best-effort: attaching or a listener firing must never raise into the caller.
    """
    if not is_event_bus_enabled():
        return

    from tabvis.browser.observation import emit_observation

    def _publish(obs_type: str, payload: dict[str, Any]) -> None:
        try:
            event = build_observation(obs_type, payload, agent_id=agent_id, session_id=session_id)
            task = asyncio.ensure_future(emit_observation(event))
            _pending_tasks.add(task)
            task.add_done_callback(_pending_tasks.discard)
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[OBS-ADAPTER] publish failed: {e}")

    def _on_download(download: Any) -> None:
        _publish(
            ObservationType.ARTIFACT_DOWNLOADED,
            {"url": getattr(download, "url", None), "filename": getattr(download, "suggested_filename", None)},
        )

    def _on_console(message: Any) -> None:
        try:
            if getattr(message, "type", None) == "error":
                _publish(ObservationType.CONSOLE_ERROR, {"text": getattr(message, "text", None)})
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[OBS-ADAPTER] console handler failed: {e}")

    try:
        page.on("download", _on_download)
        page.on("console", _on_console)
    except Exception as e:  # noqa: BLE001 - never let listener setup break a launch
        log_for_debugging(f"[OBS-ADAPTER] attach failed: {e}")
