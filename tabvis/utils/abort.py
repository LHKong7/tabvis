"""AbortController / AbortSignal shim — Python analogue of the web/Node API.

The TS tree threads an ``AbortController`` through ``ToolUseContext`` and cancels in-flight
work via ``signal``. This mirrors the surface (``signal.aborted``, ``throw_if_aborted``,
listeners, awaitable ``wait``) on top of :mod:`asyncio`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


class AbortError(Exception):
    """Raised when an aborted signal is checked via :meth:`AbortSignal.throw_if_aborted`."""


class AbortSignal:
    def __init__(self) -> None:
        self._aborted = False
        self._reason: BaseException | None = None
        self._event = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> BaseException | None:
        return self._reason

    def throw_if_aborted(self) -> None:
        if self._aborted:
            raise self._reason or AbortError("This operation was aborted")

    def add_event_listener(self, _type: str, callback: Callable[[], None]) -> None:
        if self._aborted:
            callback()
            return
        self._listeners.append(callback)

    async def wait(self) -> None:
        """Resolve when the signal is aborted."""
        await self._event.wait()

    def _abort(self, reason: BaseException | None) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason or AbortError("This operation was aborted")
        self._event.set()
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001 - listeners must not break abort
                pass
        self._listeners.clear()


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: BaseException | None = None) -> None:
        self.signal._abort(reason)
