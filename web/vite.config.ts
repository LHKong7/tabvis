import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'

// The Python server serves tabvis/browser/static/index.html at `/` (see tabvis/browser/server.py).
// We build a SINGLE self-contained index.html — all JS + CSS inlined — so it drops into that path
// with no new routes and the console keeps its "no CDN, works offline" property.
//
//   npm run dev    → Vite dev server with HMR; API calls proxied to the running tabvis server.
//   npm run build  → typecheck, then emit ../tabvis/browser/static/index.html.
//
// Dev API target: override with TABVIS_SERVER=http://host:port if the server isn't on 127.0.0.1:8765.
const API_BACKEND = process.env.TABVIS_SERVER || 'http://127.0.0.1:8765'

// Everything the console talks to is same-origin in production; in dev we proxy these prefixes.
const proxy = Object.fromEntries(
  ['/health', '/config', '/agent', '/agents', '/browser', '/browsers', '/workspaces', '/executions', '/v1'].map(
    (p) => [p, { target: API_BACKEND, changeOrigin: true }],
  ),
)

export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: {
    // Emit straight into the directory the Python server already serves.
    outDir: '../tabvis/browser/static',
    emptyOutDir: true, // replaces the old single-file console + the legacy vendored React/htm
  },
  server: { proxy },
})
