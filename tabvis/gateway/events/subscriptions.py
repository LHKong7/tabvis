"""In-memory live fan-out (design §3.1 Event Bus, §5.3).

The second layer of the split bus: a low-latency notifier for currently-connected subscribers. It is
deliberately *not* authoritative — a subscriber that misses a live event recovers it by replaying from
its last cursor against the durable :class:`~tabvis.gateway.events.store.EventStore`. So this layer may
drop, reorder under load, or restart with zero correctness impact; the cursor is the source of truth.

A subscriber typically does: read the durable backlog after its cursor, then attach to the live bus
for everything newer — the "replay then live" pattern (design §9.5). :func:`resume_point` returns the
cursor to attach at so the two halves join without a gap or a duplicate.
"""

from __future__ import annotations

from typing import Callable

from tabvis.gateway.protocol.events import EventEnvelope
from tabvis.utils.debug import log_for_debugging

Listener = Callable[[EventEnvelope], None]


class LiveBus:
    """A minimal synchronous fan-out. Listeners are called in subscription order; a failure is isolated."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return _unsubscribe

    def listener_count(self) -> int:
        return len(self._listeners)

    def publish(self, envelope: EventEnvelope) -> None:
        """Notify every listener. Never raises — a bad listener must not break the producer."""
        for listener in list(self._listeners):
            try:
                listener(envelope)
            except Exception as e:  # noqa: BLE001 - one bad listener must not break publish
                log_for_debugging(f"[GATEWAY-BUS] listener failed: {e}")


_bus: LiveBus | None = None


def get_live_bus() -> LiveBus:
    """The process-wide :class:`LiveBus`."""
    global _bus
    if _bus is None:
        _bus = LiveBus()
    return _bus


def reset_live_bus() -> None:
    """Drop the process-wide bus (tests)."""
    global _bus
    _bus = None
