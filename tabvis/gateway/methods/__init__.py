"""Command methods — one handler per command type, behind a router (design §3.1, §9.4).

The Command Router maps a versioned command name to exactly one :class:`CommandHandler` and enforces
idempotency for a ``command_id`` (design §3.1, §5.5): a duplicate returns the original result rather
than mutating twice. Handlers route to the runtime services (RunStore, InteractionService,
orchestrator) — a handler never runs the model loop or a browser operation itself (Phase 3 acceptance,
§15).
"""

from __future__ import annotations
