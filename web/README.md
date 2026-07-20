# Tabvis agent console (React front-end)

A **Vite + React + TypeScript** console for the tabvis HTTP/SSE API. tabvis itself is **headless —
it ships no built-in UI**. You get a console one of two ways: run it live in dev, or build it and
host the static bundle yourself (pointing it at the tabvis API).

## Develop

Install once:

```bash
cd web && npm install
```

### Option A — one command (recommended)

```bash
uv run tabvis --serve --dev        # or TABVIS_WEB_DEV=1 uv run tabvis --serve
```

The Python server starts Vite (`npm run dev`) for you and **reverse-proxies the console to it**, so
the whole app is live at `http://127.0.0.1:8765/` on one origin — edit `web/src/*` and the browser
hot-reloads. API routes are served by the Python server directly; the HMR websocket goes straight to
Vite on `:5173`. `--dev` needs Node/npm and `web/node_modules`; without them it fails loud.

### Option B — Vite standalone

```bash
npm run dev                        # Vite + HMR on http://localhost:5173
```

Here Vite proxies the API (`/health`, `/config`, `/agents`, `/agent` SSE, `/browsers`, …) to a
tabvis server you run separately (`uv run tabvis --serve` on `:8765`). Point the proxy elsewhere with
`TABVIS_SERVER=http://host:port npm run dev`; move Vite's port with `TABVIS_WEB_DEV_PORT=…`.

## Build & host it yourself

```bash
cd web
npm run build        # tsc --noEmit, then a standard bundle in web/dist/
```

`web/dist/` is a plain static bundle — serve it from any static host / CDN / your own reverse proxy,
and make sure its API calls (`/health`, `/config`, `/agents`, `/agent` SSE, `/browsers`, …) reach a
running tabvis server. Simplest: host `web/dist` and the tabvis API under the **same origin** (a
reverse proxy that sends `/health`, `/config`, `/agent*`, `/browsers*`, `/workspaces*`, `/executions*`
to tabvis and everything else to the static bundle) so no CORS is needed. `web/dist/` is **not**
committed and **not** bundled into the Python package — tabvis serves no UI.

## Layout

```
web/
  index.html            Vite entry / bundle entry
  vite.config.ts        standard build (-> web/dist) + dev API proxy + HMR
  src/
    main.tsx            React root
    App.tsx             layout + polling + run lifecycle
    api.ts              typed fetch client + SSE reader (runAgent)
    types.ts            API response types
    format.ts           ms()/clock()/summarize() helpers
    index.css           all styles
    components/         NewRun, AgentList, Stream, Detail, Settings, Driver, Setup, Health, Banner, Code
```
