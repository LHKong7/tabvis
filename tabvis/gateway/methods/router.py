"""CommandRouter — dispatch a command to its handler, idempotently (design §3.1, §5.5).

The router owns two cross-cutting concerns so handlers don't repeat them:

* **Dispatch** — one handler per command type; an unknown type is ``UNKNOWN_COMMAND``.
* **Idempotency** — before dispatching, the router checks the ``commands`` ledger for this
  ``command_id`` and, on a hit, returns the stored result marked ``duplicate`` without re-running the
  handler (design §5.5). On a miss it dispatches and records the result. Handlers additionally make
  their own mutations idempotent (a run is keyed by its creating command; an interaction records its
  answering command), so a crash between mutation-commit and ledger-write cannot double-apply.

  The ledger is keyed by ``(command_id, principal)``, NOT by ``command_id`` alone. ``command_id``
  is client-supplied (``x-tabvis-command-id``), and the replay short-circuits ahead of the handler,
  which is where authorization lives — so a ``command_id``-only key let any principal read back
  another principal's result (a run record, prompt included) simply by reusing their id. A hit
  belonging to someone else is treated as a miss and dispatched normally, which is the correct
  per-principal semantics; the ledger row is then left alone by the best-effort recorder.
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


# Recorded inside the ledger's ``result`` JSON blob rather than as a column: the gateway migrator is
# additive-tables-only and has no ALTER TABLE path, so a record field is the supported way to add
# one (CLAUDE.md, "Two SQLite stores"). Stripped before the result is handed back to a caller.
_PRINCIPAL_KEY = "_principal_id"


def _replay_belongs_to(prior: dict, principal: Principal) -> bool:
    """Whether ``principal`` may read back this ledger entry.

    Rows written before the principal was recorded carry no owner. Only an admin may replay those —
    an admin can already reach every resource, so it changes nothing for the loopback console, while
    a scoped agent principal re-dispatches instead of reading a stranger's result.
    """
    owner = prior.get(_PRINCIPAL_KEY)
    if owner is None:
        return principal.is_admin
    return owner == principal.principal_id


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
        # Idempotent replay: a previously-seen command returns its original result (design §5.5),
        # but only to the principal that issued it.
        prior = db.get_command_result(command.command_id)
        if prior is not None and _replay_belongs_to(prior, ctx.principal):
            return CommandResult(
                command_id=command.command_id,
                status=prior.get("status", "accepted"),
                data=prior.get("data", {}),
                duplicate=True,
            )
        handler = self.handler_for(command.type)
        result = await handler.handle(command, ctx)
        self._record(command, result, ctx.principal)
        return result

    def _record(
        self, command: Command, result: CommandResult, principal: Principal
    ) -> None:
        """Persist the command result for idempotent replay. Best-effort — the mutation already
        committed, and each handler is independently idempotent, so a ledger hiccup is not fatal."""
        try:
            with db.transaction() as conn:
                if db.get_command_result(command.command_id) is None:
                    db.insert_command_result(
                        conn,
                        command.command_id,
                        command.type,
                        {**result.to_dict(), _PRINCIPAL_KEY: principal.principal_id},
                        _utc_now(),
                    )
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[GATEWAY] failed to record command {command.command_id}: {e}")
