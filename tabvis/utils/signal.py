"""Tiny listener-set primitive for pure event signals

Collapses the duplicated ``const listeners = new Set(); subscribe(){…}; notify(){for(const l
of listeners) l()}`` boilerplate into a one-liner. Distinct from a store (AppState,
create_store) — there is no snapshot, no get_state. Use this when subscribers only need to
know "something happened", optionally with event args, not "what is the current value".

Casing: Python identifiers are snake_case; the ``Signal`` shape (``subscribe``/``emit``/
``clear``) maps to a small dataclass exposing those three callables, so callers keep
``sig.subscribe(...)`` / ``sig.emit(...)`` parity with the TS object-literal return.

Stdlib name note: this is ``tabvis.utils.signal`` — a fully namespaced module. ``import signal``
elsewhere still resolves to the stdlib ``signal`` (Python 3 absolute imports); there is no
shadowing.

Faithful-behavior notes:
- TS iterates a JS ``Set`` (insertion-ordered, dedupes identical listener references). We back
  it with a plain ``list`` for ordered iteration but de-dupe on subscribe so the same listener
  isn't registered twice (matching ``Set.add`` semantics). The returned unsubscribe removes
  the first matching registration.
- ``emit`` calls listeners synchronously in subscription order, forwarding ``*args`` verbatim.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Signal:
    """A pure event signal: subscribe listeners, emit to all, or clear them.

    Attributes:
        subscribe: register a listener ``(*args) -> None``; returns an unsubscribe callable.
        emit: invoke every subscribed listener with the given ``*args``.
        clear: remove all listeners (useful in dispose/reset paths).
    """

    subscribe: Callable[[Callable[..., None]], Callable[[], None]]
    emit: Callable[..., None]
    clear: Callable[[], None]


def create_signal() -> Signal:
    """Create a fresh :class:`Signal` with its own private listener set."""
    listeners: list[Callable[..., None]] = []

    def subscribe(listener: Callable[..., None]) -> Callable[[], None]:
        # JS Set.add dedupes identical references; mirror that.
        if listener not in listeners:
            listeners.append(listener)

        def unsubscribe() -> None:
            try:
                listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def emit(*args: Any) -> None:
        # Iterate a snapshot so a listener that (un)subscribes during emit is well-defined.
        for listener in list(listeners):
            listener(*args)

    def clear() -> None:
        listeners.clear()

    return Signal(subscribe=subscribe, emit=emit, clear=clear)
