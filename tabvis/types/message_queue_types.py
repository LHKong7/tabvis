"""Message-queue transcript types

The TS module's sole *explicit* export is::

    export type MessageQueueEntry = Record<string, unknown>

which maps to a plain open ``dict[str, Any]`` (modeled below as a ``TypedDict`` documenting
that it is an arbitrary key/value record).

It is *also* the declared home of two queue-operation contracts that this tree's
consumers import from ``messageQueueTypes.js`` — ``utils/messageQueueManager.ts``
(``logOperation`` builds a ``QueueOperationMessage`` and passes a ``QueueOperation`` literal)
and ``types/logs.ts`` (``QueueOperationMessage`` is a member of the transcript ``Entry`` union).
The ``.ts`` file in this snapshot was reduced to the ``MessageQueueEntry`` one-liner, so the
two referenced types are reconstructed here verbatim from their unambiguous use sites:

* ``QueueOperation`` — the operation kind logged for each queue mutation. The literals passed
  to ``logOperation`` in ``messageQueueManager.ts`` are ``'enqueue'`` / ``'dequeue'`` /
  ``'remove'`` / ``'popAll'`` (lines 130-471).
* ``QueueOperationMessage`` — the transcript entry appended by ``recordQueueOperation``
  (``sessionStorage.ts:1441``), built in ``messageQueueManager.logOperation`` as
  ``{ type: 'queue-operation', operation, timestamp: new Date().toISOString(), sessionId,
  ...(content !== undefined && { content }) }``.

Casing convention (``docs/SPINE_CONTRACTS.md``): Python identifiers are snake_case; dict-shaped
data that round-trips to the transcript keeps its wire keys. These are transcript envelopes, so
they are modeled as ``TypedDict`` (the ``message.py`` envelope style) with verbatim wire keys —
``type`` / ``operation`` / ``timestamp`` / ``sessionId`` / ``content`` — and NO ``extra=forbid``
(the transcript stays an open record).
"""

from __future__ import annotations

from typing import Literal, TypedDict

__all__ = [
    "MessageQueueEntry",
    "QueueOperation",
    "QUEUE_OPERATIONS",
    "QueueOperationMessage",
]


# ``Record<string, unknown>`` — an arbitrary, open key/value record. ``total=False`` with no
# declared keys models "any string key, any value": every key is optional and extra keys are
# accepted at runtime (TypedDicts do not reject unknown keys).
class MessageQueueEntry(TypedDict, total=False):
    """An opaque message-queue entry (``Record<string, unknown>``)."""


# The operation kind recorded for every command-queue mutation. Faithful to the literals
# passed into ``messageQueueManager.logOperation``.
QueueOperation = Literal["enqueue", "dequeue", "remove", "popAll"]

QUEUE_OPERATIONS: tuple[QueueOperation, ...] = (
    "enqueue",
    "dequeue",
    "remove",
    "popAll",
)
"""Runtime tuple of the :data:`QueueOperation` literal members (for membership checks)."""


class QueueOperationMessage(TypedDict, total=False):
    """A ``'queue-operation'`` transcript entry appended on each queue mutation.

    Built by ``messageQueueManager.logOperation`` and persisted via
    ``recordQueueOperation``. Wire keys are kept verbatim (camelCase ``sessionId``). ``content``
    is optional — it is only spread in when ``content !== undefined`` (string command values).
    """

    type: Literal["queue-operation"]  # required discriminant
    operation: QueueOperation  # required
    timestamp: str  # required — new Date().toISOString()
    sessionId: str  # required — getSessionId()
    content: str  # optional — only present for string command values
