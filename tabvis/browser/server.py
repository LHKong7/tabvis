"""HTTP/SSE server — drive the agent over the network and stream its events back.

``POST /agent`` starts one agent session and streams every event back as Server-Sent Events, so a
client (a web UI, a script, ``curl``) can watch the agent think, call tools, drive the browser, and
finish — live, without polling.

Run it::

    uv run tabvis --serve                       # 127.0.0.1:8765
    uv run tabvis --serve --port 9000 --host 0.0.0.0

Then::

    curl -N -X POST localhost:8765/agent \
         -H 'content-type: application/json' \
         -d '{"prompt": "open example.com and tell me the heading"}'

Endpoints
---------
* ``POST /agent`` (alias ``POST /agents``) — run one agent; stream it back as SSE. The new
  ``agent_id`` comes back in the ``X-Agent-Id`` header and the first ``agent`` frame.
* ``GET  /agents`` — list every agent run (``?status=`` / ``?limit=``).
* ``GET  /agents/{id}`` — one agent's full record.
* ``POST /agents/{id}/cancel`` — stop a running agent.
* ``GET  /agents/{id}/browser`` — that agent's browser-session record.
* ``GET  /health`` — fleet view: running / capacity / total.

Event stream
------------
Each frame is ``event: <name>`` + ``data: <json>``. The stream is the agent's real SDKMessage
stream, plus flattened convenience events so a frontend does not have to walk content blocks:

* ``agent``       — the AgentRecord, first, so the client learns its ``agent_id``.
* ``system``      — session init (session id, model, tools).
* ``assistant``   — a full assistant message.
* ``tool_use``    — flattened: ``{id, name, input}`` for each tool the agent invokes.
* ``tool_result`` — flattened: ``{tool_use_id, is_error, content}`` for each result.
* ``delta``       — partial streaming chunks (``stream_event``), when ``stream: true``.
* ``result``      — the terminal message (final text, usage, ``is_error``).
* ``error`` / ``cancelled`` — the run raised, or was cancelled. Terminal.
* ``done``        — always last, so clients know the stream closed cleanly.

Concurrency
-----------
Agents run **in parallel**, each with its own Chromium (``services/browser/manager.py`` keeps one
``BrowserService`` per agent, selected by a ContextVar). Isolation is by **profile directory**,
because Chromium takes a single-writer lock on one:

* omit ``profile`` → an isolated profile per agent; any number can run at once.
* ``profile: "default"`` → the shared, logged-in browser. Only ONE live agent may hold it; a
  second request for it gets ``409`` naming the holder.

``TABVIS_SERVER_MAX_AGENTS`` (default 4) caps the fleet — each agent is a real browser process — and
an over-cap request gets ``429``. Each agent's browser is closed when its run ends, releasing the
profile for the next agent; the cleanup registry is drained once, at server shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from typing import Any

from tabvis.ui.entry import config_api
from tabvis.agent.agents import registry
from tabvis.agent.agents.registry import AgentRecord
from tabvis.utils.debug import log_for_debugging

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Each running agent owns a real Chromium, so cap the fleet. Override: TABVIS_SERVER_MAX_AGENTS.
DEFAULT_MAX_AGENTS = 4

# Stand-in for base64 image bytes stripped out of the stream (see _redact_images).
_ELIDED = "<elided:image-bytes>"


def _sse(event: str, data: Any) -> dict[str, str]:
    """One SSE frame, in the shape sse-starlette's EventSourceResponse expects."""
    return {"event": event, "data": json.dumps(data, default=str)}


async def _ws_pump(websocket: Any, queue: "asyncio.Queue[Any]") -> None:
    """Drain observation frames onto a WebSocket until cancelled or the socket breaks (RT-2)."""
    while True:
        frame = await queue.get()
        try:
            await websocket.send_json(frame)
        except Exception:  # noqa: BLE001 - broken socket: stop pumping, let events_ws tear down
            break


def _redact_images(message: dict[str, Any]) -> dict[str, Any]:
    """Copy of ``message`` with base64 image payloads stripped out.

    A single browser screenshot is ~18-40KB of base64. It is genuinely useful to the *model*, but
    shipping it down the SSE wire floods the stream and helps no client. Replace the bytes with a
    marker, keeping the block's shape so consumers can still see that an image was returned.
    Copy-on-write: only the containers along the path are rebuilt, and the original message (which
    the agent still uses) is never mutated.
    """
    inner = message.get("message")
    if not isinstance(inner, dict) or not isinstance(inner.get("content"), list):
        return message

    new_content: list[Any] = []
    changed = False
    for block in inner["content"]:
        if not isinstance(block, dict):
            new_content.append(block)
            continue
        # An image block can sit directly in the content, or nested inside a tool_result.
        if block.get("type") == "image":
            new_content.append({**block, "source": {**(block.get("source") or {}), "data": _ELIDED}})
            changed = True
        elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
            sub = []
            for b in block["content"]:
                if isinstance(b, dict) and b.get("type") == "image":
                    sub.append({**b, "source": {**(b.get("source") or {}), "data": _ELIDED}})
                    changed = True
                else:
                    sub.append(b)
            new_content.append({**block, "content": sub})
        else:
            new_content.append(block)

    if not changed:
        return message
    return {**message, "message": {**inner, "content": new_content}}


def _flatten(message: dict[str, Any]) -> list[dict[str, str]]:
    """Turn one SDKMessage into the SSE frames a client actually wants."""
    mtype = message.get("type")

    if mtype == "stream_event":
        return [_sse("delta", message.get("event", message))]

    # Emit the raw message WITHOUT image bytes — the un-redacted message carries the full base64
    # screenshot and would otherwise dominate the stream.
    frames: list[dict[str, str]] = [_sse(mtype or "message", _redact_images(message))]

    # Flatten tool_use / tool_result out of the content blocks so a frontend need not walk them.
    inner = message.get("message") or {}
    content = inner.get("content") if isinstance(inner, dict) else None
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                frames.append(
                    _sse(
                        "tool_use",
                        {
                            "id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        },
                    )
                )
            elif btype == "tool_result":
                body = block.get("content")
                # A tool_result's content may be a string or a list of blocks (e.g. a screenshot).
                # Never ship base64 image bytes down the SSE stream — summarize them instead.
                if isinstance(body, list):
                    parts = []
                    for b in body:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "text":
                            parts.append(b.get("text", ""))
                        elif b.get("type") == "image":
                            parts.append("<image>")
                    body = "\n".join(parts)
                frames.append(
                    _sse(
                        "tool_result",
                        {
                            "tool_use_id": block.get("tool_use_id"),
                            "is_error": bool(block.get("is_error")),
                            "content": body,
                        },
                    )
                )
    return frames


def _count_tool_uses(message: dict[str, Any]) -> int:
    inner = message.get("message") or {}
    content = inner.get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return 0
    return sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use")


async def _run_agent(record: AgentRecord, queue: asyncio.Queue[Any]) -> None:
    """Drive one agent run, pushing SSE frames onto ``queue``. Runs as its own task.

    Decoupling the run from the HTTP connection is what makes the lifecycle real: the registry
    holds this task, so ``POST /agents/<id>/cancel`` can actually stop it, and the run survives a
    client disconnect long enough to record a terminal state.
    """
    from tabvis.ui.cli.print import stream_agent
    from tabvis.browser.manager import get_session_summary

    result_text: str | None = None
    is_error = False

    # OBS-5: when the Event Bus is on, forward THIS run's semantic observations as `observation` SSE
    # frames (filtered by agent_id). No-op with the flag off, so the default stream is unchanged.
    unsubscribe = None
    try:
        from tabvis.browser.event_bus import get_event_bus, is_event_bus_enabled

        if is_event_bus_enabled():
            from tabvis.browser.observation import (
                OBSERVATION_TYPES,
                install_observation_pipeline,
            )

            install_observation_pipeline()

            async def _observation_sink(event: Any) -> None:
                if event.agent_id == record.agent_id and event.type in OBSERVATION_TYPES:
                    await queue.put(_sse("observation", event.to_dict()))

            unsubscribe = get_event_bus().subscribe(_observation_sink)
    except Exception:  # noqa: BLE001 - the observation stream is best-effort
        unsubscribe = None

    try:
        await registry.mark_running(record)
        await queue.put(_sse("agent", record.to_dict()))  # so the client learns its agent_id

        async for message in stream_agent(
            record.prompt,
            model=record.model,
            max_turns=record.max_turns,
            include_partial_messages=record.stream_partials,
            agent_id=record.agent_id,
            profile=record.profile,
            session_id=record.session_id,
            resume=record.resume,  # a reused agent replays its session's prior turns
            teardown=False,  # close only this agent's browser; registry drains at shutdown
            run_id=record.run_id,
            resume_mode=record.resume_mode,
            principal_id=record.principal_id,
            write_memory=(record.resume_mode != "conversation_only"),
        ):
            mtype = message.get("type")
            if mtype == "assistant":
                record.turns += 1
                record.tool_calls += _count_tool_uses(message)
            elif mtype == "result":
                result_text = message.get("result")
                is_error = bool(message.get("is_error"))
            record.browser = get_session_summary(record.agent_id)

            for frame in _flatten(message):
                await queue.put(frame)

        await registry.mark_finished(
            record,
            status="failed" if is_error else "completed",
            result=result_text,
            error=result_text if is_error else None,
            is_error=is_error,
        )
    except asyncio.CancelledError:
        await registry.mark_finished(record, status="cancelled", error="cancelled by request")
        await queue.put(_sse("cancelled", {"agent_id": record.agent_id}))
        raise
    except Exception as e:  # noqa: BLE001 - surface to the client, don't 500 mid-stream
        log_for_debugging(f"[SERVER] agent {record.agent_id} failed: {e}")
        await registry.mark_finished(
            record, status="failed", error=f"{type(e).__name__}: {e}", is_error=True
        )
        await queue.put(_sse("error", {"message": f"{type(e).__name__}: {e}"}))
    finally:
        if unsubscribe is not None:
            unsubscribe()  # OBS-5: stop forwarding observations for this finished run
        await queue.put(_sse("done", {"agent_id": record.agent_id, "status": record.status}))
        await queue.put(None)  # sentinel: closes the SSE stream


async def _agent_events(record: AgentRecord) -> Any:
    """Drain the run's queue into SSE frames.

    The run lives in its own task (see :func:`_run_agent`), so a client disconnect here does not
    kill it — the agent finishes and records a terminal state either way. Nothing is ever yielded
    from a ``finally``: during teardown that raises "async generator ignored GeneratorExit" and
    aborts the unwind.
    """
    queue: asyncio.Queue[Any] = asyncio.Queue()
    task = asyncio.ensure_future(_run_agent(record, queue))
    registry.bind_task(record.agent_id, task)
    try:
        while True:
            frame = await queue.get()
            if frame is None:
                return
            yield frame
    finally:
        # Client hung up (or the stream closed). The run itself keeps going — that is deliberate:
        # cancel it explicitly via POST /agents/<id>/cancel if you want it stopped.
        pass


def config_readiness() -> dict[str, Any]:
    """Can this server actually run an agent? Booleans only — never echo the credential.

    The console reads this to tell you *before* you hit "Run" that (say) TABVIS_BASE_URL is missing,
    instead of letting every run die with an opaque API error.
    """
    from tabvis.utils.browser_config import (
        BROWSER_ENGINE_CATALOG,
        camoufox_available,
        cloakbrowser_available,
        engine_package_available,
        get_browser_cdp_endpoint,
        get_browser_engine,
        get_browser_user_data_dir,
        get_browser_ws_endpoint,
        get_cloak_license_key,
        get_engine_spec,
        is_browser_headless,
        playwright_available,
    )

    base_url = bool(os.environ.get("TABVIS_BASE_URL"))
    credential = bool(os.environ.get("TABVIS_API_KEY") or os.environ.get("TABVIS_AUTH_TOKEN"))
    missing = [
        name
        for name, ok in (("TABVIS_BASE_URL", base_url), ("TABVIS_API_KEY", credential))
        if not ok
    ]
    engine = get_browser_engine()
    spec = get_engine_spec(engine)
    # Whether the CURRENT engine can actually launch: its backing package is installed, and any
    # endpoint its mode requires is set. The console reads this to warn BEFORE the run, instead of
    # letting the first BrowserNavigate be where you find out.
    pkg_ok = engine_package_available(spec.requires)
    endpoint_ok = (
        (spec.mode != "cdp" or bool(get_browser_cdp_endpoint()))
        and (spec.mode != "connect" or bool(get_browser_ws_endpoint()))
    )
    return {
        "ready": base_url and credential,
        "missing": missing,
        "base_url": base_url,
        "credential": credential,
        "model": os.environ.get("TABVIS_MODEL") or "default",
        "playwright": playwright_available(),
        "browser_engine": engine,
        # The full compatibility matrix, so the console can offer every engine and label each with
        # its kernel / connection mode / stealth flag / required package.
        "browser_engines": [
            {
                "key": s.key,
                "label": s.label,
                "kernel": s.kernel,
                "browser_type": s.browser_type,
                "mode": s.mode,
                "stealth": s.stealth,
                "requires": s.requires,
                "notes": s.notes,
            }
            for s in BROWSER_ENGINE_CATALOG.values()
        ],
        "browser_type": spec.browser_type,
        "browser_kernel": spec.kernel,
        "browser_mode": spec.mode,
        "browser_stealth": spec.stealth,
        "browser_engine_requires": spec.requires,
        "engine_package_ready": pkg_ok,
        "engine_endpoint_ready": endpoint_ok,
        "engine_ready": pkg_ok and endpoint_ok,
        # Booleans only: whether a package/endpoint/key is set, never the value itself.
        "cloakbrowser": cloakbrowser_available(),
        "camoufox": camoufox_available(),
        "cloak_ready": engine != "cloak" or cloakbrowser_available(),
        "cloak_licensed": bool(get_cloak_license_key()),
        "browser_cdp_endpoint_set": bool(get_browser_cdp_endpoint()),
        "browser_ws_endpoint_set": bool(get_browser_ws_endpoint()),
        "browser_headless": is_browser_headless(),
        "browser_profile_dir": get_browser_user_data_dir(),
        "max_agents": max_concurrent_agents(),
    }


def max_concurrent_agents() -> int:
    """Cap on simultaneously-running agents — each one owns a real Chromium."""
    try:
        return max(1, int(os.environ.get("TABVIS_SERVER_MAX_AGENTS") or DEFAULT_MAX_AGENTS))
    except ValueError:
        return DEFAULT_MAX_AGENTS


def _gateway_enabled() -> bool:
    """Whether to mount the Agent Gateway control plane. ``TABVIS_GATEWAY`` (default ON)."""
    val = os.environ.get("TABVIS_GATEWAY")
    if val is None:
        return True
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _gateway_agents_enabled() -> bool:
    """Whether the legacy ``/agents`` surface is served by the gateway (retiring the registry).

    ``TABVIS_GATEWAY_AGENTS`` (default OFF). When on (and the gateway is mounted), the agent lifecycle
    endpoints are backed by gateway Run data instead of the ``AgentRecord`` registry (design §9.8).
    """
    val = os.environ.get("TABVIS_GATEWAY_AGENTS")
    return bool(val) and val.strip().lower() not in ("0", "false", "no", "off", "")


def create_app(auth_required: bool = False, dev: bool = False) -> Any:
    """Build the Starlette app (imports are local so importing this module stays cheap).

    ``auth_required`` (set by :func:`serve_async` for a non-loopback bind) makes the management face
    reject unauthenticated requests and enforce per-agent isolation. The default (False) preserves the
    open loopback/dev posture — an unauthenticated caller is the local admin.

    ``dev`` (``--serve --dev``) starts the Vite dev server from ``web/`` and reverse-proxies the
    console to it (live HMR from source). Without ``--dev`` tabvis serves NO built-in UI — it is a
    headless JSON/SSE API and ``/`` returns a pointer to the two ways to get a console. API routes
    are unaffected; under ``--dev`` only ``/`` and unmatched frontend asset paths proxy to Vite.
    """
    from sse_starlette.sse import EventSourceResponse
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    from tabvis.browser.server_auth import SecurityMiddleware, resolve_principal
    from tabvis.policy.runtime_adapter import authorize_agent, filter_visible_agents
    from tabvis.browser.manager import (
        close_browser,
        get_profile_holder,
        get_workspace_owner,
        list_workspaces,
        resolve_profile_dir,
    )

    def _principal(request: Request) -> Any:
        """The request's Principal, or None when auth is required but the request is unauthenticated."""
        return resolve_principal(request.headers, auth_required=auth_required)

    def _guard_agent(request: Request, action: str, agent_id: str) -> JSONResponse | None:
        """Return a 401/403 response if the caller may not touch ``agent_id``; else None (allowed).

        Ownership is checked before existence so a non-owner cannot probe which agent ids exist.
        """
        principal = _principal(request)
        if principal is None:
            return JSONResponse({"error": "authentication required"}, status_code=401)
        decision = authorize_agent(principal, action, agent_id)
        if decision.get("behavior") != "allow":
            return JSONResponse({"error": decision.get("message", "forbidden")}, status_code=403)
        return None

    async def console(_request: Request) -> Any:
        """GET / — tabvis serves NO built-in web UI; this is a JSON/SSE API. Point the user at the
        two supported ways to get a console. (Under ``--dev`` this handler is replaced by a live-Vite
        reverse proxy; see create_app.)"""
        from starlette.responses import JSONResponse as _JSONResponse

        return _JSONResponse(
            {
                "service": "tabvis",
                "ui": "none — this is a headless JSON/SSE API",
                "get_a_console": [
                    "run `tabvis --serve --dev` for the live React console (Vite HMR from web/)",
                    "or build web/ (`cd web && npm run build`) and host web/dist behind your own "
                    "server, pointing it at this API",
                ],
                "api": ["GET /health", "GET/POST /config", "POST /agent (SSE)", "GET /agents"],
            },
            status_code=404,
        )

    async def health(_request: Request) -> JSONResponse:
        running = registry.running_count()
        return JSONResponse(
            {
                "status": "ok",
                "running": running,
                "max_agents": max_concurrent_agents(),
                "capacity": max(0, max_concurrent_agents() - running),
                "agents": len(registry.list_agents()),
                "browsers": len(list_workspaces()),   # persistent workspaces still open
                "config": config_readiness(),
            }
        )

    def _fresh(record: AgentRecord) -> dict[str, Any]:
        """Record + a live browser view (the stored copy only refreshes on each message)."""
        from tabvis.browser.manager import get_session_summary

        data = record.to_dict()
        if not record.is_terminal:
            data["browser"] = get_session_summary(record.agent_id) or data.get("browser") or {}
        return data

    async def get_config(request: Request) -> JSONResponse:
        """Current settings. Secrets report set/not-set + a mask — never the value."""
        local = config_api.writes_allowed(request.client.host if request.client else None)
        return JSONResponse(
            {
                # Even the masked hint is withheld from remote callers — it would leak the last
                # four characters of a live credential.
                "settings": config_api.read_config(reveal_hint=local),
                "writable": local,
                "env_file": config_api.env_path() if local else "",
            }
        )

    async def put_config(request: Request) -> JSONResponse:
        """Apply settings live (effective next run) and persist them to .env."""
        host = request.client.host if request.client else None
        if not config_api.writes_allowed(host):
            return JSONResponse(
                {
                    "error": "config changes are only allowed from localhost. This server has no "
                    "authentication, so a remote client must not be able to set a credential or "
                    "repoint TABVIS_BASE_URL. Set TABVIS_SERVER_ALLOW_REMOTE_CONFIG=1 to override."
                },
                status_code=403,
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)

        values = payload.get("values")
        if not isinstance(values, dict):
            return JSONResponse({"error": "'values' object is required"}, status_code=400)
        try:
            result = config_api.apply_config(values)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        # Hand back the new readiness so the console can clear its warning immediately.
        return JSONResponse({**result, "config": config_readiness()})

    async def list_drivers_route(_request: Request) -> JSONResponse:
        """The browser-driver catalog with per-driver install state (for the console's driver picker)."""
        from tabvis.browser.drivers import list_drivers

        return JSONResponse(await list_drivers())

    async def install_driver_route(request: Request) -> Any:
        """Download a Playwright browser (chromium/firefox/webkit) via `playwright install`, streaming
        progress back as SSE (event: progress | result | done).

        Loopback-only, like config writes: running an install subprocess is a privileged action, so a
        remote unauthenticated client must not be able to trigger it.
        """
        host = request.client.host if request.client else None
        if not config_api.writes_allowed(host):
            return JSONResponse(
                {"error": "driver install is only allowed from localhost."}, status_code=403
            )
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)

        from tabvis.browser.drivers import install_browser_stream, install_via

        browser = (payload.get("browser") or "").strip().lower()
        if install_via(browser) is None:  # clean 400 before switching to the event stream
            return JSONResponse(
                {
                    "error": f"'{browser}' is not a downloadable driver — system browsers "
                    "(chrome/brave/…) are installed by you; remote engines use an endpoint."
                },
                status_code=400,
            )

        async def events() -> Any:
            async for ev in install_browser_stream(browser):
                kind = ev.get("type")
                if kind == "progress":
                    yield _sse("progress", {"text": ev.get("text", "")})
                elif kind == "result":
                    yield _sse(
                        "result",
                        {k: ev.get(k) for k in ("ok", "browser", "installed", "message")},
                    )
            yield _sse("done", {})

        return EventSourceResponse(events())

    async def list_browsers(_request: Request) -> JSONResponse:
        """Every persistent browser workspace still open (they outlive the runs that used them)."""
        ws = list_workspaces()
        return JSONResponse({"browsers": ws, "count": len(ws)})

    async def close_browser_route(request: Request) -> JSONResponse:
        """Close a persistent workspace.

        Body: ``{"user_data_dir": "…"}`` targets one exactly (what ``GET /browsers`` returns for each
        workspace — the console uses this so a Close button hits the row it is shown against). Or
        ``{"profile": "default"}`` resolves by profile (omit => the isolated one).
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        profile = payload.get("profile")
        user_data_dir = payload.get("user_data_dir") or resolve_profile_dir(
            payload.get("agent_id") or "", profile
        )
        holder = get_profile_holder(user_data_dir)
        if holder is not None:
            return JSONResponse(
                {"error": f"agent {holder!r} is driving that browser right now", "held_by": holder},
                status_code=409,
            )
        closed = await close_browser(user_data_dir)
        return JSONResponse({"closed": closed, "profile": profile or "isolated"})

    async def list_agents(request: Request) -> JSONResponse:
        status = request.query_params.get("status")
        limit_raw = request.query_params.get("limit")
        try:
            limit = int(limit_raw) if limit_raw else None
        except ValueError:
            limit = None
        principal = _principal(request)
        if principal is None:
            return JSONResponse({"error": "authentication required"}, status_code=401)
        records = registry.list_agents(status=status, limit=limit)
        # Enforce visibility by ownership, not by a query filter — a caller sees only its own runs.
        visible = set(filter_visible_agents(principal, [r.agent_id for r in records]))
        records = [r for r in records if r.agent_id in visible]
        return JSONResponse({"agents": [_fresh(r) for r in records], "count": len(records)})

    async def get_agent(request: Request) -> JSONResponse:
        denied = _guard_agent(request, "runtime.read", request.path_params["agent_id"])
        if denied is not None:
            return denied
        record = registry.get(request.path_params["agent_id"])
        if record is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        return JSONResponse(_fresh(record))

    async def cancel_agent(request: Request) -> JSONResponse:
        agent_id = request.path_params["agent_id"]
        denied = _guard_agent(request, "runtime.cancel", agent_id)
        if denied is not None:
            return denied
        record = registry.get(agent_id)
        if record is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        if record.is_terminal:
            return JSONResponse(
                {"error": f"agent is already {record.status}", "status": record.status},
                status_code=409,
            )
        await registry.cancel(agent_id)
        return JSONResponse({"agent_id": agent_id, "status": record.status})

    async def quit_agent(request: Request) -> JSONResponse:
        """Quit an agent and close its bundled browser — the 'user quit them' action.

        Unlike cancel this also works on a *finished* agent that is still holding its browser open
        (the bundle outlives the run): it closes the browser and frees the profile for a new agent.
        """
        agent_id = request.path_params["agent_id"]
        denied = _guard_agent(request, "runtime.manage", agent_id)
        if denied is not None:
            return denied
        record = registry.get(agent_id)
        if record is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        await registry.quit(agent_id)
        return JSONResponse({"agent_id": agent_id, "status": record.status, "quit": True})

    async def agent_artifacts(request: Request) -> JSONResponse:
        """The agent's browsing trail: navigation / page / interaction / DOM artifacts.

        ``GET /agents/<id>/artifacts`` → the event log + a summary. ``?dom=<ref>`` fetches one stored
        DOM blob (the ``dom_ref`` an event carries). ``?limit=N`` returns only the last N events.
        """
        from tabvis.browser.artifacts import (
            artifacts_summary,
            load_artifacts,
            read_dom,
        )

        agent_id = request.path_params["agent_id"]
        denied = _guard_agent(request, "runtime.read", agent_id)
        if denied is not None:
            return denied
        record = registry.get(agent_id)
        if record is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)

        dom_ref = request.query_params.get("dom")
        if dom_ref:
            html = read_dom(dom_ref, record.session_id)
            if html is None:
                return JSONResponse({"error": "unknown dom_ref"}, status_code=404)
            return JSONResponse({"dom_ref": dom_ref, "html": html})

        events = load_artifacts(record.session_id)
        limit_raw = request.query_params.get("limit")
        if limit_raw:
            try:
                events = events[-max(0, int(limit_raw)) :]
            except ValueError:
                pass
        return JSONResponse(
            {
                "agent_id": agent_id,
                "summary": artifacts_summary(record.session_id),
                "artifacts": events,
                "count": len(events),
            }
        )

    async def agent_browser(request: Request) -> JSONResponse:
        """The full browser-session record for one agent."""
        from tabvis.browser.manager import get_session_record

        agent_id = request.path_params["agent_id"]
        denied = _guard_agent(request, "runtime.read", agent_id)
        if denied is not None:
            return denied
        if registry.get(agent_id) is None:
            return JSONResponse({"error": "unknown agent_id"}, status_code=404)
        record = get_session_record(agent_id)
        return JSONResponse(record.to_dict() if record is not None else {})

    async def browser_session(_request: Request) -> JSONResponse:
        """The CLI/default agent's browser record (kept for compatibility)."""
        from tabvis.browser.manager import get_session_record

        record = get_session_record()
        return JSONResponse(record.to_dict() if record is not None else {})

    async def workspace_snapshot(request: Request) -> JSONResponse:
        """A first-class Workspace snapshot: agent_id + task + pages + artifacts (WS-1).

        ``GET /workspaces/{id}/snapshot`` returns the design's Workspace view (Agent ID / Goal /
        Task / Pages / Artifacts / Timeline) for a ``workspace_id`` minted at spawn.
        """
        from tabvis.browser import workspace as ws_module

        snap = ws_module.snapshot(request.path_params["workspace_id"])
        if snap is None:
            return JSONResponse({"error": "unknown workspace_id"}, status_code=404)
        return JSONResponse(snap)

    async def list_workspaces_route(_request: Request) -> JSONResponse:
        """Every first-class workspace, as snapshots (WS-6)."""
        from tabvis.browser import workspace as ws_module

        snaps = ws_module.list_workspace_snapshots()
        return JSONResponse({"workspaces": snaps, "count": len(snaps)})

    async def pause_workspace(request: Request) -> JSONResponse:
        """Pause a workspace — the browser stays open, the workspace is marked paused (WS-6)."""
        from tabvis.browser import workspace as ws_module

        record = ws_module.pause(request.path_params["workspace_id"])
        if record is None:
            return JSONResponse({"error": "unknown workspace_id"}, status_code=404)
        return JSONResponse({"workspace_id": record.workspace_id, "status": record.status})

    async def close_workspace_route(request: Request) -> JSONResponse:
        """Close a workspace: close its browser and free the profile (WS-6)."""
        from tabvis.browser import workspace as ws_module

        workspace_id = request.path_params["workspace_id"]
        if ws_module.get_workspace(workspace_id) is None:
            return JSONResponse({"error": "unknown workspace_id"}, status_code=404)
        closed = await ws_module.close_workspace(workspace_id)
        return JSONResponse({"workspace_id": workspace_id, "closed": closed})

    async def register_agent(request: Request) -> JSONResponse:
        """RT-3: register an agent and mint a local credential (an Agent Context)."""
        from tabvis.agent.agents import credentials

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        result = credentials.register(
            cwd=payload.get("cwd") or os.getcwd(),
            model=payload.get("model"),
            profile=payload.get("profile"),
        )
        return JSONResponse(result)

    async def get_execution(request: Request) -> JSONResponse:
        """RT-4 / INT-6: one execution record by id."""
        from tabvis.browser.intents import get_execution_registry

        record = get_execution_registry().get(request.path_params["execution_id"])
        if record is None:
            return JSONResponse({"error": "unknown execution_id"}, status_code=404)
        return JSONResponse(record.to_dict())

    async def cancel_execution(request: Request) -> JSONResponse:
        """RT-4 / INT-6: cancel a still-running execution (best-effort — executions are sync today)."""
        from tabvis.browser.intents import get_execution_registry

        execution_id = request.path_params["execution_id"]
        cancelled = get_execution_registry().cancel(execution_id)
        return JSONResponse({"execution_id": execution_id, "cancelled": cancelled})

    async def agent_identity(request: Request) -> JSONResponse:
        """RT-4: the agent's BrowserIdentity metadata (refs only, never secrets)."""
        from tabvis.browser import identity_store

        agent_id = request.path_params["agent_id"]
        denied = _guard_agent(request, "runtime.read", agent_id)
        if denied is not None:
            return denied
        identity = identity_store.get_by_agent(agent_id)
        if identity is None:
            return JSONResponse({"error": "no identity for agent"}, status_code=404)
        return JSONResponse(identity.metadata())

    async def workspace_intents(request: Request) -> JSONResponse:
        """RT-4: run an intent in a workspace — delegates to the IntentRouter, returns the execution."""
        from tabvis.browser import workspace as ws_module
        from tabvis.browser.intents import Intent, get_intent_router
        from tabvis.browser.manager import bind_agent, unbind_agent

        workspace_id = request.path_params["workspace_id"]
        record = ws_module.get_workspace(workspace_id)
        if record is None:
            return JSONResponse({"error": "unknown workspace_id"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        intent_name = (payload.get("intent") or "").strip()
        if not intent_name:
            return JSONResponse({"error": "'intent' is required"}, status_code=400)
        params = {k: v for k, v in payload.items() if k != "intent"}
        token = bind_agent(record.agent_id)
        try:
            result = await get_intent_router().route(
                Intent(name=intent_name, params=params),
                agent_id=record.agent_id,
                workspace_id=workspace_id,
            )
        finally:
            unbind_agent(token)
        return JSONResponse(result.to_dict())

    async def events_ws(websocket: Any) -> None:
        """RT-2: stream semantic observations over WebSocket (``/v1/events``).

        Subscribes to the in-process EventBus and forwards observation events, optionally filtered by
        ``?agent_id``. Silent when the bus is off (``TABVIS_BROWSER_EVENT_BUS``). The client need not
        send anything — a receive loop just detects the disconnect.
        """
        from tabvis.browser.event_bus import get_event_bus
        from tabvis.browser.observation import (
            OBSERVATION_TYPES,
            install_observation_pipeline,
        )

        await websocket.accept()
        want_agent = websocket.query_params.get("agent_id")
        # Bounded so a slow/wedged client cannot make the sink grow the queue without limit; a full
        # queue drops the newest frame (best-effort observation stream) rather than block the producer.
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)

        async def _sink(event: Any) -> None:
            if event.type in OBSERVATION_TYPES and (not want_agent or event.agent_id == want_agent):
                try:
                    queue.put_nowait(event.to_dict())
                except asyncio.QueueFull:
                    pass

        install_observation_pipeline()
        unsubscribe = get_event_bus().subscribe(_sink)
        pump = asyncio.ensure_future(_ws_pump(websocket, queue))
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
        except Exception:  # noqa: BLE001 - client hung up
            pass
        finally:
            unsubscribe()
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump  # observe the cancelled pump so a send error isn't logged as unretrieved
            with contextlib.suppress(Exception):
                await websocket.close()

    async def run_agent(request: Request) -> Any:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not (payload.get("prompt") or "").strip():
            return JSONResponse({"error": "'prompt' is required"}, status_code=400)

        # RT-3: a registration credential (X-Tabvis-Agent-Credential) supplies the agent_id from an
        # authenticated Agent Context — a body agent_id may not override it. Without a credential the
        # server behaves exactly as before. A registered agent already exists, so the reuse path picks
        # it up.
        from tabvis.agent.agents import credentials

        cred_agent = credentials.agent_id_from_request_headers(request.headers)
        if cred_agent:
            body_agent = (payload.get("agent_id") or "").strip()
            if body_agent and body_agent != cred_agent:
                return JSONResponse(
                    {"error": "agent_id in the body does not match the presented credential"},
                    status_code=403,
                )
            payload = {**payload, "agent_id": cred_agent}

        def _at_capacity() -> JSONResponse | None:
            # Each running agent owns a real Chromium — cap how many run at once.
            if registry.running_count() >= max_concurrent_agents():
                return JSONResponse(
                    {
                        "error": "at capacity — too many agents running",
                        "running": registry.running_count(),
                        "max_agents": max_concurrent_agents(),
                    },
                    status_code=429,
                )
            return None

        # --- REUSE an existing agent -------------------------------------------------------
        # An agent is a durable, reusable entity: pass its `agent_id` to run another prompt on the
        # SAME agent — same session (transcript continues) and same bundled browser/profile. Its id
        # and session persist across process restarts (records are loaded from disk on demand).
        requested_id = (payload.get("agent_id") or "").strip()
        if requested_id:
            existing = registry.get(requested_id)
            if existing is None:
                return JSONResponse(
                    {"error": f"unknown agent_id {requested_id!r}; omit it to create a new agent"},
                    status_code=404,
                )
            if existing.status == "running":
                return JSONResponse(
                    {"error": f"agent {requested_id!r} is already running", "status": "running"},
                    status_code=409,
                )
            cap = _at_capacity()
            if cap is not None:
                return cap
            # Resume recovery (§5.2): if this agent's bundled browser is still live, the reuse
            # reattaches the exact workspace; otherwise it will relaunch the persistent profile.
            from tabvis.browser.manager import get_browser_service

            svc = get_browser_service(requested_id)
            recovery = (
                "attached_live" if (svc is not None and svc.is_alive()) else "relaunched_profile"
            )
            resume_mode = str(payload.get("resume_mode") or "plus").strip() or "plus"
            if resume_mode not in ("plus", "conversation_only"):
                return JSONResponse(
                    {"error": f"invalid resume_mode {resume_mode!r}; "
                     "expected 'plus' or 'conversation_only'"},
                    status_code=400,
                )
            # Its profile is its own bundle, so no profile-conflict check — reuse keeps it.
            record = registry.reuse(
                requested_id,
                prompt=payload["prompt"],
                model=payload.get("model"),
                max_turns=payload.get("max_turns"),
                stream_partials=bool(payload.get("stream", False)),
                resume_mode=resume_mode,
                browser_recovery=recovery,
            )
            return EventSourceResponse(
                _agent_events(record), headers={"X-Agent-Id": record.agent_id}
            )

        # --- CREATE a new agent ------------------------------------------------------------
        cap = _at_capacity()
        if cap is not None:
            return cap

        # An agent BUNDLES a browser profile for its whole life, so a profile is off-limits while
        # any agent owns it — not just while a run is mid-flight. Two agents can run in parallel iff
        # they own different Chromium profile dirs (Chromium locks a profile to one process). An
        # isolated (omitted) profile is per-agent, so it never collides.
        profile = payload.get("profile")
        agent_id = registry.new_agent_id()
        owner = get_workspace_owner(resolve_profile_dir(agent_id, profile))
        if owner is not None:
            return JSONResponse(
                {
                    "error": f"browser profile {profile or 'default'!r} is bundled to agent "
                    f"{owner!r} for its whole life. Use a different profile, omit it for an "
                    f"isolated one, or quit that agent (POST /agents/{owner}/quit).",
                    "held_by": owner,
                },
                status_code=409,
            )

        record = registry.create(
            agent_id=agent_id,  # the id we reserved for the profile-conflict check above
            session_id=str(uuid.uuid4()),  # minted here so it's on the record before the run
            prompt=payload["prompt"],
            model=payload.get("model"),
            max_turns=payload.get("max_turns"),
            profile=profile,
            cwd=os.getcwd(),
            stream_partials=bool(payload.get("stream", False)),
        )
        return EventSourceResponse(
            _agent_events(record), headers={"X-Agent-Id": record.agent_id}
        )

    # --dev: the Vite dev server subprocess, started/stopped by the lifespan below.
    _dev_server = None
    if dev:
        from tabvis.browser.dev_server import ViteDevServer

        _dev_server = ViteDevServer()

    @contextlib.asynccontextmanager
    async def lifespan(_app: Any) -> Any:
        try:
            if _dev_server is not None:
                await _dev_server.start()  # fail loud if npm/web deps are missing
            yield
        finally:
            if _dev_server is not None:
                await _dev_server.stop()
            # Drain the gateway (stop accepting, close its store) before the browser cleanup.
            gw = getattr(_app.state, "gateway", None)
            if gw is not None:
                try:
                    gw.drain()
                except Exception:  # noqa: BLE001 - best-effort
                    pass
            # Drain the cleanup registry ONCE, at process exit — this is what closes the browser and
            # finalizes the session record. Per-request runs pass teardown=False to stay warm.
            try:
                from tabvis.utils.cleanup_registry import run_cleanup_functions

                await asyncio.wait_for(run_cleanup_functions(), timeout=10.0)
            except Exception:  # noqa: BLE001 - best-effort
                pass

    # Registry-retirement cutover (design §9.8): when TABVIS_GATEWAY_AGENTS is on and the gateway is
    # mounted, the agent lifecycle endpoints are served by gateway Run data instead of the AgentRecord
    # registry. Default off, so the registry-backed path above is unchanged.
    if _gateway_enabled() and _gateway_agents_enabled():
        from tabvis.gateway.access.legacy_agents import gateway_agent_handlers

        _gw_agent = gateway_agent_handlers()
        run_agent = _gw_agent["run_agent"]
        list_agents = _gw_agent["list_agents"]
        get_agent = _gw_agent["get_agent"]
        cancel_agent = _gw_agent["cancel_agent"]

    # RT-1: one declarative table of API routes, each mounted at BOTH its legacy path and a ``/v1``
    # alias (same handler, byte-identical response), so the versioned Runtime API surface can grow
    # additively (design.md §"Runtime API 形态"). The console ``/`` is intentionally not versioned.
    api_routes = [
        ("/health", health, ["GET"]),
        ("/config", get_config, ["GET"]),
        ("/config", put_config, ["POST"]),
        # Run one agent (SSE). Also accepted at /agents for symmetry with the collection.
        ("/agent", run_agent, ["POST"]),
        ("/agents", run_agent, ["POST"]),
        # Manage agents by id.
        ("/agents", list_agents, ["GET"]),
        ("/agents/{agent_id}", get_agent, ["GET"]),
        ("/agents/{agent_id}/cancel", cancel_agent, ["POST"]),
        ("/agents/{agent_id}/quit", quit_agent, ["POST"]),
        ("/agents/{agent_id}/browser", agent_browser, ["GET"]),
        ("/agents/{agent_id}/artifacts", agent_artifacts, ["GET"]),
        # First-class workspaces (WS-1 / WS-6).
        ("/workspaces", list_workspaces_route, ["GET"]),
        ("/workspaces/{workspace_id}/snapshot", workspace_snapshot, ["GET"]),
        ("/workspaces/{workspace_id}/pause", pause_workspace, ["POST"]),
        ("/workspaces/{workspace_id}/close", close_workspace_route, ["POST"]),
        # RT-3 / RT-4: registration, intents, executions, identity (delegating shells).
        ("/agents/register", register_agent, ["POST"]),
        ("/workspaces/{workspace_id}/intents", workspace_intents, ["POST"]),
        ("/executions/{execution_id}", get_execution, ["GET"]),
        ("/executions/{execution_id}/cancel", cancel_execution, ["POST"]),
        ("/agents/{agent_id}/identity", agent_identity, ["GET"]),
        # Persistent browser workspaces — they outlive individual runs.
        ("/browsers", list_browsers, ["GET"]),
        ("/browsers/drivers", list_drivers_route, ["GET"]),
        ("/browsers/install", install_driver_route, ["POST"]),
        ("/browsers/close", close_browser_route, ["POST"]),
        ("/browser/session", browser_session, ["GET"]),
    ]
    # The console at `/`: served from the built bundle, or (--dev) reverse-proxied to Vite.
    if dev:
        from tabvis.browser.dev_server import proxy_to_vite

        console_route = Route("/", proxy_to_vite, methods=["GET", "HEAD"])
    else:
        console_route = Route("/", console, methods=["GET"])
    routes = [console_route]  # the console UI (unversioned)
    for path, handler, methods in api_routes:
        routes.append(Route(path, handler, methods=methods))
        routes.append(Route("/v1" + path, handler, methods=methods))
    routes.append(WebSocketRoute("/v1/events", events_ws))  # RT-2: observation event channel

    # Mount the Agent Gateway control plane (docs/AGENT_GATEWAY_DESIGN.md) additively: the new /v1
    # command surface (/v1/runs, /v1/events SSE, interactions, conversations) is served alongside the
    # legacy /v1/agents API, and Runs execute through the real agent loop via AgentRunLauncher. The
    # gateway health lives at /v1/gateway/health so the legacy /v1/health is untouched, and the SSE
    # GET /v1/events coexists with the legacy WebSocket at the same path (different scope types). Set
    # TABVIS_GATEWAY=0 to disable.
    gateway_app = None
    if _gateway_enabled():
        from tabvis.gateway.access.http import gateway_routes
        from tabvis.gateway.lifecycle import GatewayApplication
        from tabvis.gateway.runtime.agent import AgentRunLauncher
        from tabvis.gateway.runtime.context.sources import SourceCollector

        # The launcher assembles a Context Pack from live sources and injects its situational sections
        # into the model's system prompt (design §11 → model call path), observable via context.pack.built.
        gateway_app = GatewayApplication.build(
            host="0.0.0.0" if auth_required else "127.0.0.1",
            launcher=AgentRunLauncher(context_collector=SourceCollector()),
        )
        gateway_app.startup()
        # include_compat=False: the legacy server still owns /v1/agents with its registry-backed
        # handlers; the gateway's projection of that surface (design §9.8) is served by the standalone
        # gateway app until a deliberate cutover.
        routes.extend(gateway_routes(health_path="/v1/gateway/health", include_compat=False))

    if dev:
        # Catch-all LAST so API routes win; forwards Vite's module graph (/src/*, /@vite/*, …) to it.
        routes.append(Route("/{path:path}", proxy_to_vite, methods=["GET", "HEAD"]))

    app = Starlette(routes=routes, lifespan=lifespan)
    # P0-2: transport hardening (security headers, body cap, default-deny CORS) — pure ASGI, so the
    # SSE/streaming routes are untouched.
    app.add_middleware(SecurityMiddleware)
    if gateway_app is not None:
        app.state.gateway = gateway_app  # the gateway route handlers read this
    return app


def _resolve(host: str | None, port: int | None) -> tuple[str, int]:
    host = host or os.environ.get("TABVIS_SERVER_HOST") or DEFAULT_HOST
    if port is None:
        try:
            port = int(os.environ.get("TABVIS_SERVER_PORT") or DEFAULT_PORT)
        except ValueError:
            port = DEFAULT_PORT
    return host, port


async def serve_async(host: str | None = None, port: int | None = None, dev: bool = False) -> None:
    """Run the SSE server on the CALLER's event loop.

    ``uvicorn.run()`` calls ``asyncio.run()`` internally, which explodes here — the whole CLI
    already runs inside ``asyncio.run(cli.main())`` (bootstrap_entry). So drive uvicorn's Server
    directly and await it on the loop we are already on.

    ``dev`` reverse-proxies the console to a live Vite dev server started from ``web/``.
    """
    import uvicorn

    from tabvis.browser.server_auth import auth_required_for_host, enforce_startup_auth

    host, port = _resolve(host, port)

    # P0-2: never expose agent management to the network without authentication.
    enforce_startup_auth(host)
    auth_required = auth_required_for_host(host)

    # RT-6: on daemon startup, reclaim crashed sessions (expired leases) so a prior crash does not
    # leave a session marked live. Best-effort — never block startup.
    try:
        import time as _time

        from tabvis.browser import session_registry

        session_registry.load_persisted_leases()
        reclaimed = session_registry.reclaim_crashed(now_ts=_time.time())
        if reclaimed:
            print(f"  reclaimed {len(reclaimed)} crashed session(s)", flush=True)
    except Exception:  # noqa: BLE001
        pass

    print(f"tabvis agent console -> http://{host}:{port}/", flush=True)
    print(f"  POST http://{host}:{port}/agent   (SSE)   GET /agents  (manage)", flush=True)
    if dev:
        print("  --dev: console served live from web/ via Vite (HMR); edits reload in the browser", flush=True)

    config = uvicorn.Config(
        create_app(auth_required=auth_required, dev=dev), host=host, port=port, log_level="warning"
    )
    await uvicorn.Server(config).serve()


def serve(host: str | None = None, port: int | None = None, dev: bool = False) -> None:
    """Blocking entry for contexts with no running loop (``python -m tabvis.browser.server``)."""
    asyncio.run(serve_async(host, port, dev=dev))


if __name__ == "__main__":  # `python -m tabvis.browser.server`
    serve()
