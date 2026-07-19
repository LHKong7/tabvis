"""AbortController factory + parent/child propagation

The TS module wraps Node's ``AbortController`` with two helpers:

- ``create_abort_controller(max_listeners=50)`` — a controller with a raised listener limit
  (avoids ``MaxListenersExceededWarning`` when many listeners attach to ``signal``).
- ``create_child_abort_controller(parent, max_listeners?)`` — a child that aborts when its
  parent aborts (propagating ``parent.signal.reason``); aborting the child does NOT affect the
  parent. When the child aborts (from any source) the parent listener is removed.

This reuses the asyncio-based shim in :mod:`tabvis.utils.abort` (``AbortController`` /
``AbortSignal``) — it does NOT reinvent an abort primitive.

Casing: Python identifiers are snake_case; constants stay UPPER_CASE (``DEFAULT_MAX_LISTENERS``).

Faithful-behavior notes:
- TS ``setMaxListeners`` tunes a Node EventEmitter to avoid a warning. The Python ``AbortSignal``
  shim stores listeners in a plain list with no cap, so ``max_listeners`` is accepted-and-ignored
  (documented no-op) — there is no equivalent warning to suppress.
- TS uses ``WeakRef`` so the parent doesn't retain an abandoned child (a JS GC concern). Python
  reference semantics differ; we register the propagation listener directly. The behavioral
  contract (child aborts on parent abort; parent listener removed once child aborts) is preserved.
  The abort shim's listeners fire once and are cleared on abort, so the {once:true} semantics hold.
- TS abort ``reason`` is read from ``parent.signal.reason`` at propagation time and forwarded to
  ``child.abort(reason)``; we mirror that.
"""

from __future__ import annotations

from tabvis.utils.abort import AbortController

# Default max listeners for standard operations.
DEFAULT_MAX_LISTENERS = 50


def create_abort_controller(
    max_listeners: int = DEFAULT_MAX_LISTENERS,
) -> AbortController:
    """Create an :class:`AbortController` (the ``max_listeners`` hint is a documented no-op)."""
    # MaxListenersExceededWarning. The Python AbortSignal shim has no listener cap / warning,
    # so this is a no-op kept for call-surface parity.
    return AbortController()


def create_child_abort_controller(
    parent: AbortController,
    max_listeners: int | None = None,
) -> AbortController:
    """Create a child controller that aborts when ``parent`` aborts.

    Aborting the child does not affect the parent. The parent listener is removed once the child
    aborts (from any source).
    """
    child = create_abort_controller(
        max_listeners if max_listeners is not None else DEFAULT_MAX_LISTENERS
    )

    # Fast path: parent already aborted, no listener setup needed.
    if parent.signal.aborted:
        child.abort(parent.signal.reason)
        return child

    def propagate_abort() -> None:
        # Forward the parent's abort reason to the child.
        child.abort(parent.signal.reason)

    # When the parent aborts, propagate to the child. The shim fires listeners once and clears
    # them on abort, so this is effectively {once: true}.
    parent.signal.add_event_listener("abort", propagate_abort)

    def remove_abort_handler() -> None:
        # Auto-cleanup: drop the parent listener once the child aborts (from any source).
        # If the parent already aborted, its listeners are cleared — harmless no-op.
        listeners = getattr(parent.signal, "_listeners", None)
        if listeners is not None:
            try:
                listeners.remove(propagate_abort)
            except ValueError:
                pass

    child.signal.add_event_listener("abort", remove_abort_handler)

    return child
