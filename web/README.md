# Tabvis agent console (React front-end)

The source for the web console served at `/` by `tabvis --serve`. It is a **Vite + React +
TypeScript** app that builds to a single self-contained `tabvis/browser/static/index.html`
(all JS + CSS inlined) — so the Python server needs no extra routes and the console keeps working
offline with no CDN.

## Develop

```bash
cd web
npm install
npm run dev          # Vite dev server + HMR on http://localhost:5173
```

The dev server proxies the API (`/health`, `/config`, `/agents`, `/agent` SSE, `/browsers`, …) to a
running tabvis server on `http://127.0.0.1:8765`. Start one in another terminal:

```bash
uv run tabvis --serve
```

Point the proxy elsewhere with `TABVIS_SERVER=http://host:port npm run dev`.

## Build (what ships)

```bash
cd web
npm run build        # tsc --noEmit, then emit ../tabvis/browser/static/index.html
```

`npm run build` **overwrites** `tabvis/browser/static/index.html` — that generated file is the
console the server serves, and it is committed so `uv`-only users need no Node toolchain. Re-run the
build and commit the result whenever you change anything under `web/src/`.

## Layout

```
web/
  index.html            Vite entry (dev only)
  vite.config.ts        single-file build + dev API proxy
  src/
    main.tsx            React root
    App.tsx             layout + polling + run lifecycle
    api.ts              typed fetch client + SSE reader (runAgent)
    types.ts            API response types
    format.ts           ms()/clock()/summarize() helpers
    index.css           all styles
    components/         NewRun, AgentList, Stream, Detail, Settings, Driver, Setup, Health, Banner, Code
```

The API contract is unchanged from the previous single-file console; only the front-end
implementation moved to a real React build.
