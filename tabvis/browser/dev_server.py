"""Dev-mode frontend: run the Vite dev server from ``web/`` and reverse-proxy the console to it.

``tabvis --serve --dev`` starts ``npm run dev`` in ``web/`` (Vite on 127.0.0.1:5173 with HMR) and the
Python server reverse-proxies every non-API request to it — so the React app is served live from
source, on the SAME origin as the API. Edit ``web/src/*`` and the browser hot-reloads; no build step.

Without ``--dev`` tabvis serves NO built-in UI (it is a headless JSON/SSE API); a console then comes
only from an external host serving a build of ``web/`` (``npm run build`` -> ``web/dist``). This
module and its Vite subprocess exist ONLY when ``--dev`` is set.

The HMR websocket connects straight to :5173 (``vite.config.ts`` sets ``server.hmr.clientPort``), so
the Python side only ever proxies plain HTTP.

Knobs: ``TABVIS_WEB_DEV`` (=1 to enable, same as ``--dev``), ``TABVIS_WEB_DIR`` (override the web/
location), ``TABVIS_WEB_DEV_HOST`` / ``TABVIS_WEB_DEV_PORT`` (where Vite binds; default 127.0.0.1:5173).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import time
from typing import Any

import httpx

VITE_HOST = os.environ.get("TABVIS_WEB_DEV_HOST") or "127.0.0.1"
VITE_PORT = int(os.environ.get("TABVIS_WEB_DEV_PORT") or "5173")
VITE_BASE = f"http://{VITE_HOST}:{VITE_PORT}"

# Hop-by-hop headers must not be forwarded verbatim; content-encoding/length are set by httpx already.
_STRIP_HEADERS = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "content-encoding", "content-length",
    }
)


def web_dir() -> str:
    """Absolute path to the ``web/`` source directory (repo root sibling of the ``tabvis`` package)."""
    override = os.environ.get("TABVIS_WEB_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))  # <repo>/tabvis/browser
    repo_root = os.path.dirname(os.path.dirname(here))  # <repo>
    return os.path.join(repo_root, "web")


_client: httpx.AsyncClient | None = None


def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # trust_env=False: the target is ALWAYS loopback Vite — never route it through an env/system
        # HTTP proxy (which would 502 a 127.0.0.1 request).
        _client = httpx.AsyncClient(timeout=30.0, trust_env=False)
    return _client


async def proxy_to_vite(request: Any) -> Any:
    """Reverse-proxy one HTTP request to the Vite dev server (the console + its module graph)."""
    from starlette.responses import PlainTextResponse, Response

    target = f"{VITE_BASE}{request.url.path}"
    if request.url.query:
        target += f"?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "connection")}
    try:
        body = await request.body()
        upstream = await _client_get().request(request.method, target, headers=headers, content=body)
    except Exception as e:  # noqa: BLE001 — Vite still starting or gone; surface a clear 502
        return PlainTextResponse(
            f"Vite dev server unavailable at {VITE_BASE} ({e}). Is `npm run dev` up?", status_code=502
        )
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _STRIP_HEADERS}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


class ViteDevServer:
    """Owns the ``npm run dev`` subprocess for the lifetime of the server process."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        wd = web_dir()
        if not os.path.isdir(wd):
            raise RuntimeError(
                f"--dev needs the web/ source directory, but {wd!r} was not found. Run tabvis from the "
                f"repo checkout, or set TABVIS_WEB_DIR."
            )
        if not os.path.isdir(os.path.join(wd, "node_modules")):
            raise RuntimeError(f"--dev needs web/ dependencies installed. Run: (cd {wd} && npm install)")
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError("--dev needs Node.js / npm on PATH (https://nodejs.org).")

        print(f"  --dev: starting Vite (npm run dev) in {wd} …", flush=True)
        self._proc = await asyncio.create_subprocess_exec(
            npm, "run", "dev", "--", "--host", VITE_HOST, "--port", str(VITE_PORT), "--strictPort",
            cwd=wd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        asyncio.create_task(self._pump_output())
        try:
            await self._wait_ready()
        except Exception:
            await self.stop()  # don't leave an orphaned Vite if startup fails
            raise
        print(f"  --dev: Vite ready — console proxied live from {VITE_BASE}", flush=True)

    async def _pump_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        async for raw in proc.stdout:
            sys.stderr.write("[vite] " + raw.decode("utf-8", "replace"))
            sys.stderr.flush()

    async def _wait_ready(self, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        client = _client_get()
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                raise RuntimeError(
                    f"Vite exited during startup (code {self._proc.returncode}); see [vite] output above."
                )
            try:
                r = await client.get(f"{VITE_BASE}/", timeout=2.0)
                if r.status_code < 500:
                    return
            except Exception:  # noqa: BLE001 — not up yet
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError(f"Vite did not become ready at {VITE_BASE} within {timeout_s:.0f}s.")

    async def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:  # noqa: BLE001 — force-kill if it won't exit cleanly
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        global _client
        if _client is not None:
            with contextlib.suppress(Exception):
                await _client.aclose()
            _client = None
