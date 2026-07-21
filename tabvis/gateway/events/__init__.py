"""Durable event bus (design §3.1, §5.3).

The design splits the old in-memory ``EventBus`` into two layers:

1. **Durable append + cursor assignment** — :class:`tabvis.gateway.events.store.EventStore`, the
   authoritative log. This is what replay reads from.
2. **In-memory live fan-out** — :mod:`tabvis.gateway.events.subscriptions`, a low-latency notifier for
   currently-connected subscribers. Fan-out loss is never fatal: a dropped live event is recovered by
   replaying from the subscriber's cursor against the durable log.
"""

from __future__ import annotations
