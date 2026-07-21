"""Interaction command handler (design §9.4, §5.2)."""

from __future__ import annotations

from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol.commands import Command, CommandResult, CommandType
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime.interaction_service import InteractionService, get_interaction_service


class InteractionRespondHandler:
    """``interaction.respond`` — answer a pending question/approval and resume its Run.

    The command_id is the response idempotency key: a duplicate response returns the original receipt
    (design §5.5, §5.2), enforced inside :class:`InteractionService`.
    """

    command_type = CommandType.INTERACTION_RESPOND

    def __init__(self, interaction_service: InteractionService | None = None) -> None:
        self._svc = interaction_service or get_interaction_service()

    async def handle(self, command: Command, ctx: CommandContext) -> CommandResult:
        interaction_id = command.data.get("interaction_id")
        if not interaction_id:
            raise GatewayError("VALIDATION_FAILED", message="interaction.respond requires 'interaction_id'")
        answer = command.data.get("answers")
        if answer is None:
            answer = command.data.get("answer", {})
        if not isinstance(answer, dict):
            raise GatewayError("VALIDATION_FAILED", message="'answers' must be an object")

        record = self._svc.get(interaction_id)
        if record is None:
            raise GatewayError("INTERACTION_NOT_FOUND", details={"interaction_id": interaction_id})
        if not ctx.principal.can_access_agent(record.agent_id):
            raise GatewayError("FORBIDDEN", details={"interaction_id": interaction_id})

        receipt = self._svc.respond(interaction_id, answer, response_command_id=command.command_id)
        return CommandResult(command.command_id, data={"interaction": receipt.to_dict()})
