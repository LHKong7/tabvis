"""``PersistenceService`` — the single write facade (PERS-1).

A pass-through today: each method delegates to the existing best-effort writer so on-disk format and
location are unchanged (``design.md`` §"Persistence Service" 当前实现). The value of the seam is
that every future write has ONE place to go through — PERS-2 adds a SQLite shadow write here, PERS-3
flips reads to it, and PERS-7 wraps the whole thing in a two-phase commit — without touching the call
sites again. Delegation targets are imported lazily inside each method to avoid import cycles
(``session`` / ``artifacts`` / ``registry`` may import persistence helpers later).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a runtime import cycle; these are only needed for type hints
    from tabvis.agent.agents.registry import AgentRecord
    from tabvis.browser.session import BrowserSessionRecord


class PersistenceService:
    """The mediating persistence facade. Stateless for now; a process singleton via

    :func:`get_persistence_service` so later steps can hang a DB connection / commit log off it.
    """

    async def save_session_record(self, record: "BrowserSessionRecord") -> None:
        """Persist the browser session record (delegates to ``session.write_browser_session``)."""
        from tabvis.browser.session import write_browser_session

        await write_browser_session(record)

    async def record_event(
        self, event: dict[str, Any], data: dict[str, Any]
    ) -> None:
        """Record one browser artifact event (delegates to ``artifacts.record_browser_artifact``)."""
        from tabvis.browser.artifacts import record_browser_artifact

        await record_browser_artifact(event, data)

    async def save_agent_record(self, record: "AgentRecord") -> None:
        """Persist an agent record (delegates to ``registry.persist``)."""
        from tabvis.agent.agents.registry import persist

        await persist(record)

    def import_legacy(self) -> None:
        """PERS-3: backfill legacy JSON records into SQLite (idempotent).

        Triggers the registry's lazy load, which mirrors any JSON-only agent records into
        ``runtime.db`` so the DB becomes complete. A no-op after the first call in a process.
        """
        from tabvis.agent.agents import registry

        registry._ensure_loaded()


_service: PersistenceService | None = None


def get_persistence_service() -> PersistenceService:
    """The process-wide :class:`PersistenceService` (lazily constructed)."""
    global _service
    if _service is None:
        _service = PersistenceService()
    return _service
