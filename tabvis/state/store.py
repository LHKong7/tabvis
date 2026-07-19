"""Generic observable store

``set_state(updater)`` replaces state via an immutable updater ``(prev) -> next``; a no-op
update (``next is prev``) is skipped (parity with TS ``Object.is``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass
class Store(Generic[T]):
    get_state: Callable[[], T]
    set_state: Callable[[Callable[[T], T]], None]
    subscribe: Callable[[Callable[[], None]], Callable[[], None]]


def create_store(
    initial_state: T,
    on_change: Callable[[dict[str, Any]], None] | None = None,
) -> Store[T]:
    box: dict[str, T] = {"state": initial_state}
    listeners: set[Callable[[], None]] = set()

    def get_state() -> T:
        return box["state"]

    def set_state(updater: Callable[[T], T]) -> None:
        prev = box["state"]
        nxt = updater(prev)
        if nxt is prev:  # Object.is(next, prev)
            return
        box["state"] = nxt
        if on_change is not None:
            on_change({"newState": nxt, "oldState": prev})
        for listener in list(listeners):
            listener()

    def subscribe(listener: Callable[[], None]) -> Callable[[], None]:
        listeners.add(listener)

        def unsubscribe() -> None:
            listeners.discard(listener)

        return unsubscribe

    return Store(get_state=get_state, set_state=set_state, subscribe=subscribe)
