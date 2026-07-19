"""In-process EventBus (OBS-2).

The single-process pub/sub the design uses to decouple Execution / Observation / Persistence
(``design.md`` §"Event Model"). Producers publish a :class:`~tabvis.browser.events.RuntimeEvent`;
sinks subscribe to receive them. It is **no-op unless enabled** (``TABVIS_BROWSER_EVENT_BUS``): with the
flag off, :meth:`EventBus.publish` returns immediately, so the whole observation pipeline is dark and
today's inline-snapshot path is byte-for-byte unchanged.

One process-wide bus is used; events carry ``agent_id`` so a per-run sink (e.g. the SSE
observation stream, OBS-5) can filter to its own run — the same isolation a per-run bus would give.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable

from tabvis.browser.events import RuntimeEvent
from tabvis.utils.debug import log_for_debugging

Sink = Callable[[RuntimeEvent], Awaitable[None]]


def is_event_bus_enabled() -> bool:
    """Whether the observation Event Bus is active. ``TABVIS_BROWSER_EVENT_BUS`` (default OFF)."""
    val = os.environ.get("TABVIS_BROWSER_EVENT_BUS")
    return bool(val) and val.strip().lower() not in ("0", "false", "no", "off", "")


class EventBus:
    """A minimal async pub/sub. Sinks are called in subscription order; a failing sink is isolated."""

    def __init__(self) -> None:
        self._sinks: list[Sink] = []

    def subscribe(self, sink: Sink) -> Callable[[], None]:
        """Register a sink; returns an unsubscribe callable."""
        self._sinks.append(sink)

        def _unsubscribe() -> None:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass

        return _unsubscribe

    def sink_count(self) -> int:
        return len(self._sinks)

    async def publish(self, event: RuntimeEvent) -> None:
        """Deliver an event to every sink. No-op when the bus is disabled; never raises."""
        if not is_event_bus_enabled():
            return
        for sink in list(self._sinks):
            try:
                await sink(event)
            except Exception as e:  # noqa: BLE001 - one bad sink must not break publish
                log_for_debugging(f"[EVENTBUS] sink failed: {e}")


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """The process-wide :class:`EventBus`."""
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


async def publish(event: RuntimeEvent) -> None:
    """Publish onto the process-wide bus (convenience)."""
    await get_event_bus().publish(event)
