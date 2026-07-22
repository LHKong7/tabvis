"""HTTP access layer — the §9.4 core methods over Starlette (design §3.1, §9).

Each route parses its input into a protocol :class:`Command`, resolves the Principal from credentials
(never the body), dispatches through the router, and renders the result or a §9.7 error body. Routes do
no domain work themselves. Transport hardening (security headers, body cap, default-deny CORS) reuses
the existing ``server_auth.SecurityMiddleware`` so the gateway's posture matches today's server.
"""

from __future__ import annotations

import secrets
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from tabvis.browser.server_auth import SecurityMiddleware
from tabvis.gateway import PROTOCOL
from tabvis.gateway.access.sse import event_stream
from tabvis.gateway.auth.authentication import resolve_principal
from tabvis.gateway.auth.principals import Principal
from tabvis.gateway.lifecycle import GatewayApplication
from tabvis.gateway.methods.router import CommandContext
from tabvis.gateway.protocol import ids
from tabvis.gateway.protocol.commands import Command, CommandType
from tabvis.gateway.protocol.compatibility import (
    legacy_frames_for,
    legacy_status,
    project_agent_list,
    project_run_as_agent,
)
from tabvis.gateway.protocol.errors import GatewayError
from tabvis.gateway.protocol.events import parse_cursor


def _error_response(err: GatewayError) -> JSONResponse:
    return JSONResponse(err.to_body(), status_code=err.http_status)


async def _principal(request: Request) -> Principal:
    return resolve_principal(request.headers, host=request.app.state.gateway.host)


def _command_id(request: Request, body: dict[str, Any]) -> str:
    return request.headers.get("x-tabvis-command-id") or body.get("command_id") or ids.new_command_id()


async def _read_json(request: Request) -> dict[str, Any]:
    if not (await request.body()):
        return {}
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise GatewayError("VALIDATION_FAILED", message="Request body must be valid JSON")
    if not isinstance(payload, dict):
        raise GatewayError("VALIDATION_FAILED", message="Request body must be a JSON object")
    return payload


async def _dispatch(request: Request, command: Command) -> Any:
    gateway: GatewayApplication = request.app.state.gateway
    principal = await _principal(request)
    ctx = CommandContext(principal=principal, trace_id=f"tr_{secrets.token_hex(4)}")
    return await gateway.router.dispatch(command, ctx)


# --- routes ------------------------------------------------------------------------------------


async def health(request: Request) -> Response:
    snapshot = request.app.state.gateway.health()
    code = 200 if snapshot["status"] in ("ready", "degraded") else 503
    return JSONResponse({"protocol": PROTOCOL, **snapshot}, status_code=code)


async def create_conversation(request: Request) -> Response:
    try:
        body = await _read_json(request)
        command = Command(type=CommandType.CONVERSATION_CREATE, data=body, command_id=_command_id(request, body))
        result = await _dispatch(request, command)
        return JSONResponse(result.to_dict(), status_code=200)
    except GatewayError as e:
        return _error_response(e)


async def create_run(request: Request) -> Response:
    try:
        body = await _read_json(request)
        command = Command(type=CommandType.RUN_CREATE, data=body, command_id=_command_id(request, body))
        result = await _dispatch(request, command)
        # 202 Accepted: the Run is created and (if a launcher is wired) executing (design §9.4).
        return JSONResponse(result.to_dict(), status_code=202)
    except GatewayError as e:
        return _error_response(e)


async def read_run(request: Request) -> Response:
    try:
        gateway: GatewayApplication = request.app.state.gateway
        principal = await _principal(request)
        run = gateway.runs.get_run(request.path_params["run_id"])
        if run is None:
            raise GatewayError("RUN_NOT_FOUND", details={"run_id": request.path_params["run_id"]})
        if not principal.can_access_agent(run.agent_id):
            # Do not leak existence to a principal that may not see it (design §13, matches server).
            raise GatewayError("FORBIDDEN", details={"run_id": run.run_id})
        return JSONResponse({"run": run.to_dict()}, status_code=200)
    except GatewayError as e:
        return _error_response(e)


async def cancel_run(request: Request) -> Response:
    try:
        body = await _read_json(request)
        data = {**body, "run_id": request.path_params["run_id"]}
        command = Command(type=CommandType.RUN_CANCEL, data=data, command_id=_command_id(request, body))
        result = await _dispatch(request, command)
        return JSONResponse(result.to_dict(), status_code=200)
    except GatewayError as e:
        return _error_response(e)


async def respond_interaction(request: Request) -> Response:
    try:
        body = await _read_json(request)
        data = {**body, "interaction_id": request.path_params["interaction_id"]}
        command = Command(type=CommandType.INTERACTION_RESPOND, data=data, command_id=_command_id(request, body))
        result = await _dispatch(request, command)
        return JSONResponse(result.to_dict(), status_code=200)
    except GatewayError as e:
        return _error_response(e)


async def subscribe_events(request: Request) -> Response:
    """SSE subscription (design §9.5). Resumes from ``cursor`` / ``Last-Event-ID``; ``follow=0`` for a
    catch-up snapshot that ends after the durable backlog."""
    from sse_starlette.sse import EventSourceResponse

    try:
        await _principal(request)  # authenticate; scoping filters below
    except GatewayError as e:
        return _error_response(e)

    gateway: GatewayApplication = request.app.state.gateway
    cursor_param = request.query_params.get("cursor") or request.headers.get("last-event-id")
    after = parse_cursor(cursor_param)
    aggregate_id = request.query_params.get("run_id")
    follow = request.query_params.get("follow", "1") not in ("0", "false", "no")

    generator = event_stream(
        gateway.events,
        after_cursor=after,
        aggregate_id=aggregate_id,
        follow=follow,
        is_disconnected=request.is_disconnected,
    )
    return EventSourceResponse(generator)


# --- IM channel ingress (design §4.5 inbound flow) ---------------------------------------------


async def channel_webhook(request: Request) -> Response:
    """POST webhook ingress for a channel — verify + normalize + start a Run (design §4.5)."""
    runtime = getattr(request.app.state.gateway, "channels", None)
    if runtime is None:
        return JSONResponse({"error": "channels not configured"}, status_code=404)
    raw = await request.body()
    try:
        result = await runtime.ingest_webhook(
            request.path_params["plugin"], request.headers, raw, dict(request.query_params)
        )
    except GatewayError as e:
        return _error_response(e)
    if result.get("challenge") is not None:
        # A POST url_verification handshake (Feishu/Slack) echoes the challenge as JSON.
        return JSONResponse({"challenge": result["challenge"]})
    return JSONResponse({"ok": True, "results": result.get("results", [])})


async def channel_webhook_verify(request: Request) -> Response:
    """GET subscription handshake for a channel (e.g. WhatsApp's ``hub.challenge``)."""
    runtime = getattr(request.app.state.gateway, "channels", None)
    if runtime is None:
        return JSONResponse({"error": "channels not configured"}, status_code=404)
    try:
        result = await runtime.ingest_webhook(
            request.path_params["plugin"], request.headers, b"", dict(request.query_params)
        )
    except GatewayError as e:
        return _error_response(e)
    challenge = result.get("challenge")
    if challenge is None:
        return Response(status_code=403)
    # A GET handshake echoes the challenge as plain text.
    return PlainTextResponse(str(challenge))


# --- legacy /agents compatibility projection (design §9.8) -------------------------------------


def _int_param(request: Request, name: str) -> int | None:
    raw = request.query_params.get(name)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


async def list_agents_compat(request: Request) -> Response:
    """Legacy ``GET /agents`` — the latest Run per agent, projected to the agent shape (design §9.8)."""
    try:
        principal = await _principal(request)
        gateway: GatewayApplication = request.app.state.gateway
        latest = [r for r in gateway.runs.latest_run_per_agent() if principal.can_access_agent(r.agent_id)]
        body = project_agent_list(latest, status=request.query_params.get("status"), limit=_int_param(request, "limit"))
        return JSONResponse(body)
    except GatewayError as e:
        return _error_response(e)


async def read_agent_compat(request: Request) -> Response:
    try:
        principal = await _principal(request)
        agent_id = request.path_params["agent_id"]
        # Ownership before existence, so a non-owner cannot probe which agent ids exist (matches legacy).
        if not principal.can_access_agent(agent_id):
            raise GatewayError("FORBIDDEN", details={"agent_id": agent_id})
        run = request.app.state.gateway.runs.latest_run_for_agent(agent_id)
        if run is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        return JSONResponse(project_run_as_agent(run))
    except GatewayError as e:
        return _error_response(e)


async def cancel_agent_compat(request: Request) -> Response:
    """Legacy ``POST /agents/{id}/cancel`` — cancel that agent's active Run (design §9.8)."""
    try:
        principal = await _principal(request)
        agent_id = request.path_params["agent_id"]
        if not principal.can_access_agent(agent_id):
            raise GatewayError("FORBIDDEN", details={"agent_id": agent_id})
        gateway: GatewayApplication = request.app.state.gateway
        run = gateway.runs.latest_run_for_agent(agent_id)
        if run is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        if run.is_terminal:
            status = legacy_status(run.status)
            return JSONResponse({"error": f"agent is already {status}", "status": status}, status_code=409)
        updated = await gateway.orchestrator.cancel(run.run_id, correlation_id=ids.new_command_id())
        return JSONResponse({"agent_id": agent_id, "status": legacy_status(updated.status)})
    except GatewayError as e:
        return _error_response(e)


async def agent_events_compat(request: Request) -> Response:
    """Legacy-named SSE frames for an agent's latest Run — v1 events projected (design §9.8)."""
    from sse_starlette.sse import EventSourceResponse

    try:
        principal = await _principal(request)
        agent_id = request.path_params["agent_id"]
        if not principal.can_access_agent(agent_id):
            raise GatewayError("FORBIDDEN", details={"agent_id": agent_id})
        gateway: GatewayApplication = request.app.state.gateway
        run = gateway.runs.latest_run_for_agent(agent_id)
        if run is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
    except GatewayError as e:
        return _error_response(e)

    async def _frames():
        import json

        for envelope in gateway.events.read(aggregate_id=run.run_id):
            for frame in legacy_frames_for(envelope):
                yield {"event": frame["event"], "data": json.dumps(frame["data"], default=str)}

    return EventSourceResponse(_frames())


def gateway_routes(*, health_path: str = "/v1/health", include_compat: bool = True) -> list[Route]:
    """The gateway's §9.4 HTTP routes, as a splice-able list.

    ``health_path`` is configurable so the routes can be mounted into an app that already owns
    ``/v1/health`` (the legacy server) without colliding — see :func:`tabvis.browser.server.create_app`.
    """
    routes = [
        Route("/v1/conversations", create_conversation, methods=["POST"]),
        Route("/v1/runs", create_run, methods=["POST"]),
        Route("/v1/runs/{run_id}", read_run, methods=["GET"]),
        Route("/v1/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        Route("/v1/interactions/{interaction_id}/responses", respond_interaction, methods=["POST"]),
        Route("/v1/events", subscribe_events, methods=["GET"]),
        # IM channel ingress: POST message webhook, GET subscription handshake (no-op unless a
        # ChannelRuntime is attached to the gateway).
        Route("/v1/channels/{plugin}/webhook", channel_webhook, methods=["POST"]),
        Route("/v1/channels/{plugin}/webhook", channel_webhook_verify, methods=["GET"]),
    ]
    if health_path:
        routes.insert(0, Route(health_path, health, methods=["GET"]))
    if include_compat:
        # Legacy /agents surface projected from gateway Run data (design §9.8). Excluded when mounting
        # into the legacy server, which still owns these paths with its registry-backed handlers.
        routes += [
            Route("/v1/agents", list_agents_compat, methods=["GET"]),
            Route("/v1/agents/{agent_id}", read_agent_compat, methods=["GET"]),
            Route("/v1/agents/{agent_id}/cancel", cancel_agent_compat, methods=["POST"]),
            Route("/v1/agents/{agent_id}/events", agent_events_compat, methods=["GET"]),
        ]
    return routes


def create_gateway_app(gateway: GatewayApplication | None = None, *, launcher: Any = None) -> Starlette:
    """Build the standalone gateway ASGI app. Pass a prebuilt ``gateway`` or let one be composed."""
    app_gateway = gateway or GatewayApplication.build(launcher=launcher)
    app_gateway.startup()
    app = Starlette(routes=gateway_routes())
    app.add_middleware(SecurityMiddleware)
    app.state.gateway = app_gateway
    return app
