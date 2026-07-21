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

Here Vite proxies the API (`/health`, `/config`, `/agents`, `/agent` SSE, `/browsers`, `/v1`, …) to a
tabvis server you run separately (`uv run tabvis --serve` on `:8765`). Point the proxy elsewhere with
`TABVIS_SERVER=http://host:port npm run dev`. `TABVIS_WEB_DEV_PORT` sets only the HMR client port
(default `5173`); to move Vite's actual listen port pass `npm run dev -- --port <n>`.

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

The console is a small `react-router-dom` app: `main.tsx` mounts the router, `App.tsx` is the shell
(nav + `<Routes>`), app-wide state and polling live in `context.tsx`, each routed view lives in
`pages/`, and the reusable panels live in `components/`.

```
web/
  index.html            Vite / bundle entry (#root + /src/main.tsx)
  vite.config.ts        build (-> web/dist) + dev API proxy + HMR clientPort
  tsconfig.json         TS config (strict, noEmit)
  src/
    main.tsx            React entry — renders <App/> inside <BrowserRouter>
    App.tsx             router shell: nav list, sidebar + <Health>, <Routes> table, wraps <AppProvider>
    context.tsx         AppProvider + useApp() — app-wide state, 1.5s /health+/agents polling, run lifecycle
    api.ts              same-origin fetch client + SSE readers (runAgent, installDriverStream)
    types.ts            API response types
    format.ts           ms() / clock() / summarize() presentation helpers
    index.css           all styles (dark default + light via prefers-color-scheme)
    pages/
      Dashboard.tsx         "/"              stat row + recent sessions
      RunPage.tsx           "/run"           new/continue-run form (<NewRun>)
      SessionsPage.tsx      "/sessions"      sessions list (<AgentList>)
      SessionDetailPage.tsx "/sessions/:id"  live <Stream> + <Detail>, polls the agent
      DriversPage.tsx       "/drivers"       browser drivers/workspaces (<Driver>; nav label "Browser")
      SettingsPage.tsx      "/settings"      settings form (<Settings>)
      SetupPage.tsx         "/setup"         run-as-a-server instructions (<Setup>)
    components/
      NewRun, AgentList, Stream, Detail, Settings, Driver, Setup, Health, Banner, Code
```

Scripts (`package.json`): `dev` (Vite + HMR), `build` (`tsc --noEmit` then bundle to `web/dist/`),
`typecheck` (`tsc --noEmit`), `preview` (`vite preview`).
