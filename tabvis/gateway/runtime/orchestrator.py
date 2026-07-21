"""RunOrchestrator — own the Run lifecycle and coordinate the runtime (design §3.1, §7).

The orchestrator is the only place Run state advances (design §7.4). It creates Runs, hands them to
an injected :class:`RunLauncher` to execute, and drives cancel/terminalization. It never formats HTTP
and never runs the model loop itself — the loop lives behind the launcher (design §3.1: "It MUST NOT
format SSE or know about React routes").

The launcher is a **seam**: wiring the real agent loop (wrapping today's ``stream_agent`` / query loop
into ``runtime/agent/runner.py``) is a later step. With no launcher, Runs are created and observable
but not executed — exactly the control-plane slice Phase 3 delivers, testable without a model.
"""

from __future__ import annotations

from typing import Protocol

from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.interaction_service import InteractionService, get_interaction_service
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.runtime.runs import RunRecord
from tabvis.utils.debug import log_for_debugging


class RunLauncher(Protocol):
    """Executes a Run's model/tool loop. Implemented by the Agent Runtime (a later phase)."""

    async def launch(self, run: RunRecord) -> None: ...

    async def abort(self, run_id: str) -> None: ...


class RunOrchestrator:
    def __init__(
        self,
        run_store: RunStore | None = None,
        interaction_service: InteractionService | None = None,
        launcher: RunLauncher | None = None,
    ) -> None:
        self._runs = run_store or get_run_store()
        self._interactions = interaction_service or get_interaction_service()
        self._launcher = launcher

    @property
    def has_launcher(self) -> bool:
        return self._launcher is not None

    async def create_and_start(
        self,
        *,
        agent_id: str,
        session_id: str,
        command_id: str,
        model: str = "",
        prompt_message_id: str = "",
        conversation_id: str | None = None,
        workspace_id: str | None = None,
        max_turns: int | None = None,
        attempt: int = 1,
    ) -> RunRecord:
        """Create a Run and, if a launcher is wired, start executing it."""
        run = self._runs.create_run(
            agent_id=agent_id, session_id=session_id, command_id=command_id, model=model,
            prompt_message_id=prompt_message_id, conversation_id=conversation_id,
            workspace_id=workspace_id, max_turns=max_turns, attempt=attempt, correlation_id=command_id,
        )
        if self._launcher is not None:
            await self._launcher.launch(run)
        return run

    async def cancel(self, run_id: str, *, correlation_id: str | None = None) -> RunRecord:
        """Cooperatively cancel a Run (design §7.6). Idempotent for an already-cancelled Run."""
        current = self._runs.get_run(run_id)
        if current is None:
            raise GatewayError("RUN_NOT_FOUND", details={"run_id": run_id})
        if current.status == runs.CANCELLED:
            return current
        if current.is_terminal:
            raise GatewayError("RUN_TERMINAL", details={"run_id": run_id, "status": current.status})

        # A waiting Run is cancelled together with the interaction it is blocked on (design §5.2, §7.6).
        if current.is_waiting:
            self._interactions.cancel_for_run(run_id, correlation_id=correlation_id)
            return self._runs.get_run(run_id) or current

        # Otherwise: persist cancelling, ask the launcher to abort, then finalize (design §7.6 steps).
        self._runs.transition(run_id, runs.CANCELLING, expected=current.status, correlation_id=correlation_id)
        if self._launcher is not None:
            try:
                await self._launcher.abort(run_id)
            except Exception as e:  # noqa: BLE001 - abort is best-effort; we still finalize the state
                log_for_debugging(f"[GATEWAY] launcher.abort failed for {run_id}: {e}")
        return self._runs.transition(
            run_id, runs.CANCELLED, expected=runs.CANCELLING,
            error_code="cancelled_by_request", correlation_id=correlation_id,
        )


_orchestrator: RunOrchestrator | None = None


def get_orchestrator() -> RunOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = RunOrchestrator()
    return _orchestrator


def set_orchestrator(orchestrator: RunOrchestrator | None) -> None:
    """Install a specific orchestrator (composition root / tests)."""
    global _orchestrator
    _orchestrator = orchestrator
