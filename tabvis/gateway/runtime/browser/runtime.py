"""BrowserRuntime — bind, observe, execute, recover (design §10.4, §10.7).

Ties identity, leases, sessions, and the driver seam into the §10.4 binding interface. It enforces the
runtime's guarantees: a binding is the only handle an agent gets; a side-effecting execution against a
disconnected session is marked ``interrupted`` rather than blindly replayed; a reconnect verifies
identity before resuming; and startup recovery reclaims only *expired* leases (design §10.7).

Every lifecycle fact is a durable event (`browser.binding.acquired/released`,
`browser.navigation.completed`, `browser.download.completed`), carrying artifact *references* — never
bytes or base64 (design §10.6).
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from tabvis.gateway.events.store import EventStore, get_event_store
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import AGGREGATE_BROWSER, EventScope, EventType
from tabvis.gateway.protocol import ids
from tabvis.gateway.runtime.browser import session as session_mod
from tabvis.gateway.runtime.browser.contracts import (
    BrowserAcquireRequest,
    BrowserBinding,
    BrowserDriver,
    BrowserIntent,
    BrowserSnapshot,
    ExecutionRecord,
)
from tabvis.gateway.runtime.browser.identity import resolve_identity
from tabvis.gateway.runtime.browser.leases import LeaseTable
from tabvis.gateway.runtime.browser.session import ArtifactStore, BrowserSession


class BrowserRuntime:
    def __init__(
        self,
        driver: BrowserDriver | None = None,
        events: EventStore | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl_seconds: float = 30.0,
    ) -> None:
        self._driver = driver
        self._events = events or get_event_store()
        self._leases = LeaseTable(clock=clock, ttl_seconds=ttl_seconds)
        self._sessions: dict[str, BrowserSession] = {}     # binding_id -> session
        self._artifacts = ArtifactStore()

    # --- binding --------------------------------------------------------------------------------

    async def acquire(self, request: BrowserAcquireRequest) -> BrowserBinding:
        identity = resolve_identity(request.agent_id, request.profile, engine=request.engine)
        lease = self._leases.acquire(
            profile_key=identity.profile_key, identity_id=identity.identity_id,
            agent_id=request.agent_id, run_id=request.run_id,
        )
        session = BrowserSession(
            session_id=ids.new_session_id().replace("ses_", "bses_"),
            binding_id=lease.binding_id, profile_key=identity.profile_key,
        )
        session.transition(session_mod.LAUNCHING)
        if self._driver is not None:
            await self._driver.launch(identity.profile_key, request.engine)
        session.transition(session_mod.READY)
        session.transition(session_mod.BUSY)  # Run binding acquired (design §10.3)
        session.open_tab()
        self._sessions[lease.binding_id] = session

        self._emit(EventType.BROWSER_BINDING_ACQUIRED, lease.binding_id, request,
                   data={"profile_key": identity.profile_key, "identity_id": identity.identity_id})
        return BrowserBinding(
            binding_id=lease.binding_id, identity_id=identity.identity_id, profile_key=identity.profile_key,
            agent_id=request.agent_id, run_id=request.run_id, engine=request.engine, expires_at=lease.expires_at,
        )

    async def release(self, binding_id: str) -> None:
        session = self._sessions.get(binding_id)
        lease = self._leases.get(binding_id)
        if session is not None and session.state == session_mod.BUSY:
            session.transition(session_mod.READY)  # Run released; profile persists past the run
        self._leases.release(binding_id)
        if lease is not None:
            self._emit(EventType.BROWSER_BINDING_RELEASED, binding_id,
                       _AgentRun(lease.agent_id, lease.run_id), data={"profile_key": lease.profile_key})

    def heartbeat(self, binding_id: str) -> str:
        return self._leases.heartbeat(binding_id).expires_at

    # --- observe --------------------------------------------------------------------------------

    def snapshot(self, binding_id: str) -> BrowserSnapshot:
        session = self._require_session(binding_id)
        d = session.snapshot_dict()
        return BrowserSnapshot(binding_id=binding_id, session_state=d["session_state"],
                               tabs=d["tabs"], artifacts=d["artifacts"], current_url=d["current_url"])

    # --- execute --------------------------------------------------------------------------------

    async def execute(self, binding_id: str, intent: BrowserIntent) -> ExecutionRecord:
        session = self._require_session(binding_id)
        lease = self._leases.get(binding_id)
        if lease is None or lease.status != "active":
            raise GatewayError("BROWSER_BINDING_NOT_FOUND", details={"binding_id": binding_id})

        # Recovery rule (design §10.7): a side-effecting execution against a disconnected session is
        # uncertain — mark it interrupted, never blindly replay.
        if session.state == session_mod.DISCONNECTED:
            if intent.side_effecting:
                return ExecutionRecord(intent=intent.action, status="interrupted",
                                       detail="session disconnected; side-effecting intent not replayed")
            raise GatewayError("BROWSER_DISCONNECTED", details={"binding_id": binding_id})

        result = await self._driver.execute(session.profile_key, intent) if self._driver else dict(intent.params)
        return self._apply_result(session, intent, result, lease)

    def _apply_result(self, session, intent, result, lease) -> ExecutionRecord:
        tab_id = session.active_tab_id
        artifact = None

        if "url" in result and tab_id:
            session.navigate(tab_id, result.get("url", ""), result.get("title", ""))
        if result.get("dom"):
            artifact = self._artifacts.put("dom", str(result["dom"]).encode("utf-8"), url=result.get("url"))
            session.add_artifact(artifact)
            self._emit(EventType.BROWSER_NAVIGATION_COMPLETED, session.binding_id,
                       _AgentRun(lease.agent_id, lease.run_id),
                       data={"url": result.get("url"), "artifact_ref": artifact.ref, "tab_id": tab_id})
        if result.get("screenshot"):
            artifact = self._artifacts.put("screenshot", bytes(result["screenshot"]) if isinstance(result["screenshot"], (bytes, bytearray)) else str(result["screenshot"]).encode())
            session.add_artifact(artifact)
        if result.get("download"):
            dl = result["download"]
            path = session.quarantine_name(dl.get("name", "download.bin"))
            content = dl.get("bytes", b"") if isinstance(dl.get("bytes"), (bytes, bytearray)) else str(dl.get("bytes", "")).encode()
            artifact = self._artifacts.put("download", content, url=dl.get("url"))
            artifact.ref = path  # quarantined path is the provenance reference
            session.add_artifact(artifact)
            self._emit(EventType.BROWSER_DOWNLOAD_COMPLETED, session.binding_id,
                       _AgentRun(lease.agent_id, lease.run_id),
                       data={"path": path, "artifact_id": artifact.artifact_id, "size_bytes": artifact.size_bytes})

        return ExecutionRecord(intent=intent.action, status="succeeded", tab_id=tab_id, artifact=artifact)

    # --- recovery -------------------------------------------------------------------------------

    def disconnect(self, binding_id: str) -> None:
        """Simulate a worker/driver drop (design §10.3 busy → disconnected)."""
        session = self._require_session(binding_id)
        if session.state in (session_mod.READY, session_mod.BUSY):
            session.transition(session_mod.DISCONNECTED)

    async def reconnect(self, binding_id: str) -> bool:
        """Verify page/context identity before resuming (design §10.7). Returns True if resumed."""
        session = self._require_session(binding_id)
        if session.state != session_mod.DISCONNECTED:
            return session.state == session_mod.BUSY
        ok = await self._driver.verify_identity(session.profile_key) if self._driver else True
        session.transition(session_mod.BUSY if ok else session_mod.FAILED)
        return ok

    def recover(self) -> list[str]:
        """Startup recovery: reclaim expired leases only; live profiles are untouched (design §10.7)."""
        return self._leases.reclaim_expired()

    async def close_identity(self, binding_id: str) -> None:
        session = self._sessions.get(binding_id)
        if session is not None and session.state not in (session_mod.CLOSED,):
            if session.state not in (session_mod.READY, session_mod.BUSY):
                session.state = session_mod.CLOSING
            else:
                session.transition(session_mod.CLOSING)
            if self._driver is not None:
                await self._driver.close(session.profile_key)
            session.transition(session_mod.CLOSED)
        self._leases.release(binding_id)

    # --- helpers --------------------------------------------------------------------------------

    def _require_session(self, binding_id: str) -> BrowserSession:
        session = self._sessions.get(binding_id)
        if session is None:
            raise GatewayError("BROWSER_BINDING_NOT_FOUND", details={"binding_id": binding_id})
        return session

    def _emit(self, event_type: str, binding_id: str, who, data: dict) -> None:
        self._events.append(
            AGGREGATE_BROWSER, binding_id, event_type,
            scope=EventScope(agent_id=getattr(who, "agent_id", None), run_id=getattr(who, "run_id", None)),
            data=data,
        )


class _AgentRun:
    """A tiny scope carrier for events emitted outside an acquire request."""

    def __init__(self, agent_id: str | None, run_id: str | None) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
