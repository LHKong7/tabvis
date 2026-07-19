"""ExecutionRegistry (INT-6) — the record of every intent execution, addressable by ``execution_id``.

``design.md`` §"Intent Router" assigns an ``execution_id`` per execution so events / artifacts / errors
correlate to it. This registry is where those :class:`~tabvis.browser.intents.types.ExecutionRecord`
land, so ``GET /v1/executions/{id}`` can return one and ``POST /v1/executions/{id}/cancel`` can mark
it. It is an in-memory ring (bounded) — durability of executions is a later concern; the point here
is a queryable, cancellable handle for a run.

Retry policy (design: "Intent Retry must be explicitly declared by the handler"): only read-only
intents are declared retryable, and nothing here auto-retries — :func:`is_retryable` just exposes the
policy for a caller to consult.
"""

from __future__ import annotations

from collections import OrderedDict

from tabvis.browser.intents.types import ExecutionRecord
from tabvis.browser.session import utc_now

# Intents safe to retry automatically (no side effects). Navigation/search change page state, so they
# are NOT retryable by default — matching the design's "don't auto-replay side-effecting operations".
_RETRYABLE = frozenset({"snapshot", "wait"})

_MAX_EXECUTIONS = 1000


def is_retryable(intent_name: str) -> bool:
    return intent_name in _RETRYABLE


class ExecutionRegistry:
    """A bounded, in-memory map of ``execution_id`` → :class:`ExecutionRecord`."""

    def __init__(self) -> None:
        self._records: "OrderedDict[str, ExecutionRecord]" = OrderedDict()

    def record(self, record: ExecutionRecord) -> ExecutionRecord:
        """Upsert an execution record (idempotent by ``execution_id``)."""
        self._records[record.execution_id] = record
        self._records.move_to_end(record.execution_id)
        while len(self._records) > _MAX_EXECUTIONS:
            self._records.popitem(last=False)
        return record

    def get(self, execution_id: str) -> ExecutionRecord | None:
        return self._records.get(execution_id)

    def list_recent(self, limit: int | None = None) -> list[ExecutionRecord]:
        """Most recent first."""
        items = list(reversed(self._records.values()))
        return items if limit is None else items[: max(limit, 0)]  # limit=0 → empty, not "all"

    def cancel(self, execution_id: str) -> bool:
        """Mark a still-running execution cancelled. False if unknown or already terminal.

        Executions are synchronous today (they complete before ``route`` returns), so this only
        affects a record still in ``running`` — the seam for a truly async execution later.
        """
        record = self._records.get(execution_id)
        if record is None or record.status != "running":
            return False
        record.status = "cancelled"
        record.ended_at = utc_now()
        record.error = "cancelled by request"
        return True


_registry: ExecutionRegistry | None = None


def get_execution_registry() -> ExecutionRegistry:
    """The process-wide :class:`ExecutionRegistry`."""
    global _registry
    if _registry is None:
        _registry = ExecutionRegistry()
    return _registry
