"""Gateway-backed legacy `/agents` endpoints — the registry retirement (design §7, §9.8, Phase 6).

The legacy agent surface is served by the **gateway's durable Agent/Run stores**, not the retired
`AgentRecord` registry: `POST /agent` creates a gateway Run (executed by the wired `AgentRunLauncher`)
and streams its events projected to legacy SSE frames; `GET /agents`, `GET /agents/{id}`, and
`POST /agents/{id}/cancel` are the durable-Agent + latest-Run projections. This is now the only path —
the gateway is the single source of truth for the agent lifecycle, and the registry is off the public
control path (the browser-bundle endpoints `/quit`, `/browser`, `/artifacts`, `/identity` resolve
existence through the durable Agent + browser subsystem, in `tabvis/browser/server.py`).
"""

from __future__ import annotations

import json
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tabvis.gateway.access.http import (
    _error_response,
    cancel_agent_compat,
    list_agents_compat,
    read_agent_compat,
)
from tabvis.gateway.auth.authentication import resolve_principal
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.compatibility import legacy_frames_for
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.runtime import runs
from tabvis.gateway.store import db

_ACTIVE_STATES = tuple(sorted(runs.ACTIVE))
_TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


def _max_agents() -> int:
    try:
        return max(1, int(os.environ.get("TABVIS_SERVER_MAX_AGENTS") or 4))
    except ValueError:
        return 4


async def run_agent_gw(request: Request) -> Response:
    """`POST /agent` — create a gateway Run and stream its events as legacy SSE frames (design §9.8)."""
    from sse_starlette.sse import EventSourceResponse

    gateway: GatewayApplication = request.app.state.gateway
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "'prompt' is required"}, status_code=400)

    try:
        principal = resolve_principal(request.headers, host=gateway.host)
    except GatewayError as e:
        return _error_response(e)

    # A per-agent credential supplies agent_id; a body agent_id may not override it (design §3.1).
    from tabvis.agent.agents import credentials

    cred_agent = credentials.agent_id_from_request_headers(request.headers)
    body_agent = (body.get("agent_id") or "").strip()
    if cred_agent:
        if body_agent and body_agent != cred_agent:
            return JSONResponse({"error": "agent_id in the body does not match the presented credential"}, status_code=403)
        body_agent = cred_agent
    if body_agent and not principal.can_access_agent(body_agent):
        return _error_response(GatewayError("FORBIDDEN", details={"agent_id": body_agent}))

    active = db.count_active_runs(_ACTIVE_STATES)
    if active >= _max_agents():
        return JSONResponse({"error": "at capacity — too many agents running", "running": active,
                             "max_agents": _max_agents()}, status_code=429)

    # Reuse an existing agent (continuation) or mint a fresh one.
    resume = False
    if body_agent:
        if gateway.runs.latest_run_for_agent(body_agent) is None:
            return JSONResponse({"error": f"unknown agent_id {body_agent!r}; omit it to create a new agent"}, status_code=404)
        agent_id, resume = body_agent, True
    else:
        agent_id = ids.new_agent_id()

    # A browser profile is bundled to an agent for its life — off-limits to another owner (design §10.5).
    profile = body.get("profile")
    try:
        from tabvis.browser.manager import get_workspace_owner, resolve_profile_dir

        owner = get_workspace_owner(resolve_profile_dir(agent_id, profile))
        if owner is not None and owner != agent_id:
            return JSONResponse({"error": f"browser profile {profile or 'default'!r} is bundled to agent {owner!r}",
                                 "held_by": owner}, status_code=409)
    except Exception:  # noqa: BLE001 - the profile check is best-effort
        pass

    command = Command(type=CommandType.RUN_CREATE, data={
        "agent_id": agent_id, "message": {"text": prompt}, "model": body.get("model"),
        "max_turns": body.get("max_turns"), "profile": profile, "resume": resume,
        "stream": bool(body.get("stream", False)),
    })
    ctx = CommandContext(principal=principal, trace_id=f"tr_{ids.new_command_id()[4:]}")
    try:
        result = await gateway.router.dispatch(command, ctx)
    except GatewayError as e:
        return _error_response(e)
    run = result.data["run"]
    return EventSourceResponse(_legacy_agent_stream(gateway, run["run_id"]), headers={"X-Agent-Id": agent_id})


async def _legacy_agent_stream(gateway: GatewayApplication, run_id: str):
    """Project a Run's events (durable replay then live) into legacy SSE frames; end on a terminal event."""
    import asyncio

    from tabvis.gateway.events.subscriptions import get_live_bus

    queue: asyncio.Queue = asyncio.Queue()

    def _listen(envelope):
        if envelope.aggregate_id == run_id:
            queue.put_nowait(envelope)

    unsubscribe = get_live_bus().subscribe(_listen)
    try:
        last = 0
        for envelope in gateway.events.read(aggregate_id=run_id):
            last = envelope.cursor
            for frame in legacy_frames_for(envelope):
                yield {"event": frame["event"], "data": json.dumps(frame["data"], default=str)}
            if envelope.type in _TERMINAL_EVENTS:
                return
        while True:
            try:
                envelope = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": ""}
                continue
            if envelope.cursor <= last or envelope.aggregate_id != run_id:
                continue
            last = envelope.cursor
            for frame in legacy_frames_for(envelope):
                yield {"event": frame["event"], "data": json.dumps(frame["data"], default=str)}
            if envelope.type in _TERMINAL_EVENTS:
                return
    finally:
        unsubscribe()


def gateway_agent_handlers() -> dict[str, Any]:
    """The gateway-backed replacements for the legacy agent handlers, keyed by role."""
    return {
        "run_agent": run_agent_gw,
        "list_agents": list_agents_compat,
        "get_agent": read_agent_compat,
        "cancel_agent": cancel_agent_compat,
    }


def build_legacy_agent_routes() -> list[Route]:
    """Standalone routes for the gateway-backed legacy agent surface (used in tests)."""
    return [
        Route("/agent", run_agent_gw, methods=["POST"]),
        Route("/agents", run_agent_gw, methods=["POST"]),
        Route("/agents", list_agents_compat, methods=["GET"]),
        Route("/agents/{agent_id}", read_agent_compat, methods=["GET"]),
        Route("/agents/{agent_id}/cancel", cancel_agent_compat, methods=["POST"]),
    ]
