"""Run command handlers (design §9.4). Handlers route to the orchestrator; they never execute a model
loop or a browser operation themselves (Phase 3 acceptance, §15)."""

from __future__ import annotations

from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.commands import Command, CommandResult, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.orchestrator import RunOrchestrator, get_orchestrator
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.store import db


def _prompt_text(data: dict) -> str:
    """Extract the user prompt from a run.create body: a ``message`` object/string or ``prompt``."""
    message = data.get("message")
    if isinstance(message, dict):
        return str(message.get("text", ""))
    if isinstance(message, str):
        return message
    return str(data.get("prompt", ""))


class RunCreateHandler:
    """``run.create`` — resolve/mint the Agent + Session, create a Run, hand it to the orchestrator.

    Idempotent on ``command_id``: a retried create returns the run the original command produced,
    never a second run (design §5.5).
    """

    command_type = CommandType.RUN_CREATE

    def __init__(self, orchestrator: RunOrchestrator | None = None) -> None:
        self._orch = orchestrator or get_orchestrator()

    async def handle(self, command: Command, ctx: CommandContext) -> CommandResult:
        data = command.data
        agent_id = data.get("agent_id") or ids.new_agent_id()
        # Identity comes from credentials, not the body: an agent principal may only create for itself.
        if not ctx.principal.can_access_agent(agent_id):
            raise GatewayError("FORBIDDEN", details={"agent_id": agent_id})

        existing = db.get_run_by_command(command.command_id)
        if existing is not None:
            return CommandResult(command.command_id, data={"run": existing}, duplicate=True)

        # Resume Plus (§12.3): a resume MUST continue the prior transcript lineage. Honor
        # ``resume_from_session_id`` (or an explicit ``session_id``) and only mint a fresh session for
        # a genuinely new conversation — the previous behavior of always minting a new session_id made
        # ``resume=True`` unable to find the earlier transcript.
        resume_mode = str(data.get("resume_mode") or "").strip()
        resume_from = data.get("resume_from_session_id")
        resume = bool(data.get("resume", False)) or bool(resume_from) or resume_mode in (
            "plus", "conversation_only",
        )
        session_id = resume_from or data.get("session_id")
        if not session_id:
            if resume:
                raise GatewayError(
                    "VALIDATION_FAILED",
                    message="a resume run requires 'resume_from_session_id' or 'session_id'",
                )
            session_id = ids.new_session_id()
        if resume and not resume_mode:
            resume_mode = "plus"

        run = await self._orch.create_and_start(
            agent_id=agent_id,
            session_id=session_id,
            command_id=command.command_id,
            model=data.get("model") or "",
            prompt_message_id=data.get("prompt_message_id") or "",
            conversation_id=data.get("conversation_id"),
            workspace_id=data.get("workspace_id"),
            max_turns=data.get("max_turns"),
            prompt=_prompt_text(data),
            profile=data.get("profile"),
            resume=resume,
            resume_mode=resume_mode or "fresh",
            stream_partials=bool(data.get("stream", False)),
        )
        return CommandResult(command.command_id, data={"run": run.to_dict()})


class RunCancelHandler:
    """``run.cancel`` — cooperatively cancel the Run (design §7.6)."""

    command_type = CommandType.RUN_CANCEL

    def __init__(self, orchestrator: RunOrchestrator | None = None, run_store: RunStore | None = None) -> None:
        self._orch = orchestrator or get_orchestrator()
        self._runs = run_store or get_run_store()

    async def handle(self, command: Command, ctx: CommandContext) -> CommandResult:
        run_id = command.data.get("run_id")
        if not run_id:
            raise GatewayError("VALIDATION_FAILED", message="run.cancel requires 'run_id'")
        current = self._runs.get_run(run_id)
        if current is None:
            raise GatewayError("RUN_NOT_FOUND", details={"run_id": run_id})
        if not ctx.principal.can_access_agent(current.agent_id):
            raise GatewayError("FORBIDDEN", details={"run_id": run_id})
        run = await self._orch.cancel(run_id, correlation_id=command.command_id)
        return CommandResult(command.command_id, data={"run": run.to_dict()})
