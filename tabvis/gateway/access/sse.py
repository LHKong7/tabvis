"""Cursor-resumable event subscription (design §5.3, §9.5).

The subscribe read is "replay then live": emit every durable event after the client's cursor, then
attach to the in-memory live fan-out for everything newer. Because replay is authoritative and the
cursor is monotonic, a reconnect at ``Last-Event-ID`` resumes with no gap and no duplicate — the same
guarantee proved at the store level, now over the wire.

``follow=False`` returns just the durable backlog and ends the stream — a catch-up snapshot, and the
form the tests drive deterministically. ``follow=True`` (the default for a live subscriber) continues
with live events until the client disconnects.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Awaitable, Callable

from tabvis.gateway.events.store import EventStore
from tabvis.gateway.events.subscriptions import LiveBus, get_live_bus
from tabvis.gateway.protocol.events import EventEnvelope, _cursor_str


def _frame(envelope: EventEnvelope) -> dict[str, Any]:
    """One SSE frame as an ``EventSourceResponse`` dict (id/event/data), per design §9.5."""
    import json

    return {
        "id": _cursor_str(envelope.cursor),
        "event": envelope.type,
        "data": json.dumps(envelope.to_dict(), default=str),
    }


async def event_stream(
    store: EventStore,
    *,
    after_cursor: int = 0,
    aggregate_id: str | None = None,
    follow: bool = True,
    live_bus: LiveBus | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    idle_tick: float = 15.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield SSE frames: durable replay after ``after_cursor``, then (if ``follow``) live events."""
    bus = live_bus or get_live_bus()
    queue: asyncio.Queue[EventEnvelope] = asyncio.Queue()
    unsubscribe: Callable[[], None] | None = None
    if follow:
        # Attach to live BEFORE the replay read so no event slips through the gap between the two;
        # the cursor filter below drops anything the replay already covered (dedupe).
        unsubscribe = bus.subscribe(queue.put_nowait)

    try:
        last = after_cursor
        for envelope in store.read(after_cursor=after_cursor, aggregate_id=aggregate_id):
            last = max(last, envelope.cursor)
            yield _frame(envelope)

        if not follow:
            return

        while True:
            if is_disconnected is not None and await is_disconnected():
                return
            try:
                envelope = await asyncio.wait_for(queue.get(), timeout=idle_tick)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}  # keep-alive; lets us re-check disconnect
                continue
            if envelope.cursor <= last:
                continue  # already delivered during replay
            if aggregate_id is not None and envelope.aggregate_id != aggregate_id:
                continue
            last = envelope.cursor
            yield _frame(envelope)
    finally:
        if unsubscribe is not None:
            unsubscribe()
