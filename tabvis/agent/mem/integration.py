"""Run-completion → consolidation glue (design §7.2, §10.4).

One entry point, :func:`consolidate_after_run`, ties the Phase 3 pipeline to a real Run: it reads the
Run's evidence, freezes an :class:`~tabvis.agent.mem.evidence.EvidenceCheckpoint`, and hands it to the
consolidator with the configured extractor. It is called from the single terminal point every surface
shares (``stream_agent``), so CLI, legacy server, and Gateway Runs all consolidate the same way.

It is doubly gated and completely best-effort:

* ``write_memory`` false (e.g. a ``conversation_only`` / ``--no-memory`` resume) → no-op;
* ``TABVIS_BROWSER_MEMORY`` off (the preview default) → no-op;
* no per-agent consent → the consolidator itself skips;
* any error is swallowed — a consolidation failure never affects the Run (§7.2).
"""

from __future__ import annotations

from typing import Any

from tabvis.agent.mem.consolidator import ConsolidationResult, consolidate_run
from tabvis.agent.mem.evidence import build_checkpoint, collect_evidence, load_run_evidence
from tabvis.agent.mem.extractor import get_extractor, is_browser_memory_enabled
from tabvis.utils.debug import log_for_debugging

_UNSET = object()


async def consolidate_after_run(
    *,
    principal_id: str,
    agent_id: str,
    session_id: str,
    run_id: str,
    status: str,
    tabs: list[dict[str, Any]] | None = None,
    browser_recovery: str | None = None,
    write_memory: bool = True,
    extractor: Any = _UNSET,
    messages: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
) -> ConsolidationResult | None:
    """Consolidate a finished Run into Agent Memory. Best-effort; returns None when gated off/failed.

    ``messages``/``artifacts`` may be supplied (tests, or a caller that already has them); otherwise
    they are loaded from disk for ``session_id``. ``extractor`` defaults to the configured one.
    """
    if not write_memory or not is_browser_memory_enabled():
        return None
    try:
        from tabvis.agent.mem.agent_store import AgentMemoryStore

        store = AgentMemoryStore.open_for(principal_id, agent_id)
        consent = store.get_consent()
        if not consent.enabled or consent.revoked:
            return None  # no consent → nothing to do (and no evidence read)

        if messages is None or artifacts is None:
            loaded_msgs, loaded_arts = await load_run_evidence(session_id)
            messages = messages if messages is not None else loaded_msgs
            artifacts = artifacts if artifacts is not None else loaded_arts

        checkpoint = build_checkpoint(
            run_id=run_id, agent_id=agent_id, session_id=session_id, status=status,
            messages=messages, artifacts=artifacts, tabs=tabs, browser_recovery=browser_recovery,
        )
        packet = collect_evidence(session_id, messages, artifacts, tabs=tabs)
        chosen = get_extractor() if extractor is _UNSET else extractor
        return await consolidate_run(store, checkpoint, packet, extractor=chosen)
    except Exception as e:  # noqa: BLE001 - consolidation never fails a Run (§7.2)
        log_for_debugging(f"[MEMORY] consolidate_after_run ignored error: {e}")
        return None
