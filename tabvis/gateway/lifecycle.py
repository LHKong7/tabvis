"""GatewayApplication — the composition root, readiness, and drain (design §2, §3, §14.1).

One object wires the stores, services, orchestrator, and router together and owns the process
lifecycle (design §2.1). The same wiring backs CLI, daemon, and tests — there is no second lifecycle
(design §2.2). ``health`` reports component-level readiness in the §2.3 shape.

The Run scheduler, worker leases, and channel startup are later phases; this root covers what the
Phase 3 control-plane slice needs: open the store, register handlers, report readiness, drain.
"""

from __future__ import annotations

from typing import Literal

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.methods.conversations import ConversationCreateHandler
from tabvis.gateway.methods.interactions import InteractionRespondHandler
from tabvis.gateway.methods.router import CommandRouter
from tabvis.gateway.methods.runs import RunCancelHandler, RunCreateHandler
from tabvis.gateway.runtime import runs
from tabvis.gateway.runtime.interaction_service import InteractionService, get_interaction_service
from tabvis.gateway.runtime.orchestrator import RunLauncher, RunOrchestrator
from tabvis.gateway.runtime.run_store import RunStore, get_run_store
from tabvis.gateway.store import db

GatewayStatus = Literal[
    "starting", "migrating", "loading", "ready", "degraded", "draining", "stopped", "failed"
]

_ACTIVE_STATES = tuple(sorted(runs.ACTIVE))


class GatewayApplication:
    def __init__(
        self,
        *,
        event_store: EventStore,
        run_store: RunStore,
        interaction_service: InteractionService,
        orchestrator: RunOrchestrator,
        router: CommandRouter,
        host: str | None = "127.0.0.1",
        max_runs: int = 4,
    ) -> None:
        self.events = event_store
        self.runs = run_store
        self.interactions = interaction_service
        self.orchestrator = orchestrator
        self.router = router
        self.host = host
        self.max_runs = max_runs
        self.status: GatewayStatus = "starting"

    # --- construction ---------------------------------------------------------------------------

    @classmethod
    def build(
        cls, *, host: str | None = "127.0.0.1", launcher: RunLauncher | None = None, max_runs: int = 4,
    ) -> "GatewayApplication":
        """Default wiring: process-wide stores/services, an orchestrator, and a router with all handlers."""
        event_store = get_event_store()
        run_store = get_run_store()
        interaction_service = get_interaction_service()
        orchestrator = RunOrchestrator(run_store, interaction_service, launcher)
        router = CommandRouter()
        router.register(RunCreateHandler(orchestrator))
        router.register(RunCancelHandler(orchestrator, run_store))
        router.register(InteractionRespondHandler(interaction_service))
        router.register(ConversationCreateHandler(event_store))
        return cls(
            event_store=event_store, run_store=run_store, interaction_service=interaction_service,
            orchestrator=orchestrator, router=router, host=host, max_runs=max_runs,
        )

    # --- lifecycle ------------------------------------------------------------------------------

    def startup(self) -> None:
        """Open the store (applying migrations) and become ready/degraded (design §2.1)."""
        self.status = "migrating"
        db.connect()  # opens the connection and runs forward-only migrations
        self.status = "loading"
        self.status = "ready" if self.orchestrator.has_launcher else "degraded"

    def drain(self) -> None:
        """Stop accepting new work and close the store (design §2.1 shutdown order, abbreviated)."""
        self.status = "draining"
        db.close()
        self.status = "stopped"

    # --- readiness ------------------------------------------------------------------------------

    def health(self) -> dict:
        """Component-level readiness, in the design §2.3 shape."""
        try:
            db.connect()
            store_state = "ready"
        except Exception:  # noqa: BLE001
            store_state = "failed"

        agent_state = "ready" if self.orchestrator.has_launcher else "not_configured"
        if store_state != "ready":
            status: GatewayStatus = "failed"
        elif agent_state != "ready":
            # Control plane is up and serving; it just won't execute Runs without a launcher.
            status = "degraded"
        else:
            status = "ready"

        active = db.count_active_runs(_ACTIVE_STATES)
        return {
            "status": status,
            "components": {
                "metadata_store": store_state,
                "event_store": store_state,
                "agent_runtime": agent_state,
                "browser_runtime": "not_configured",
                "channels": {},
            },
            "capacity": {"runs": self.max_runs, "available": max(0, self.max_runs - active)},
        }

    @property
    def is_serving(self) -> bool:
        """Whether the control plane can accept commands (ready or degraded, not failed/stopped)."""
        return self.health()["status"] in ("ready", "degraded")
