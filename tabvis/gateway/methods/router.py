"""CommandRouter — dispatch a command to its handler, idempotently (design §3.1, §5.5).

The router owns two cross-cutting concerns so handlers don't repeat them:

* **Dispatch** — one handler per command type; an unknown type is ``UNKNOWN_COMMAND``.
* **Idempotency** — before dispatching, the router checks the ``commands`` ledger for this
  ``command_id`` and, on a hit, returns the stored result marked ``duplicate`` without re-running the
  handler (design §5.5). On a miss it dispatches and records the result. Handlers additionally make
  their own mutations idempotent (a run is keyed by its creating command; an interaction records its
  answering command), so a crash between mutation-commit and ledger-write cannot double-apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from tabvis.gateway.auth.principals import Principal
from tabvis.gateway.protocol.commands import Command, CommandResult
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.store import db
from tabvis.utils.debug import log_for_debugging


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CommandContext:
    """Ambient facts a handler needs beyond the command body (design §3.1, §17)."""

    principal: Principal
    trace_id: str | None = None


class CommandHandler(Protocol):
    command_type: str

    async def handle(self, command: Command, ctx: CommandContext) -> CommandResult: ...


class CommandRouter:
    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}

    def register(self, handler: CommandHandler) -> None:
        self._handlers[handler.command_type] = handler

    def handler_for(self, command_type: str) -> CommandHandler:
        handler = self._handlers.get(command_type)
        if handler is None:
            raise GatewayError("UNKNOWN_COMMAND", details={"type": command_type})
        return handler

    async def dispatch(self, command: Command, ctx: CommandContext) -> CommandResult:
        # Idempotent replay: a previously-seen command returns its original result (design §5.5).
        prior = db.get_command_result(command.command_id)
        if prior is not None:
            return CommandResult(
                command_id=command.command_id,
                status=prior.get("status", "accepted"),
                data=prior.get("data", {}),
                duplicate=True,
            )
        handler = self.handler_for(command.type)
        result = await handler.handle(command, ctx)
        self._record(command, result)
        return result

    def _record(self, command: Command, result: CommandResult) -> None:
        """Persist the command result for idempotent replay. Best-effort — the mutation already
        committed, and each handler is independently idempotent, so a ledger hiccup is not fatal."""
        try:
            with db.transaction() as conn:
                if db.get_command_result(command.command_id) is None:
                    db.insert_command_result(conn, command.command_id, command.type, result.to_dict(), _utc_now())
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[GATEWAY] failed to record command {command.command_id}: {e}")
