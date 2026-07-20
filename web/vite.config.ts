import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// tabvis serves NO built-in UI. The console is either run live in dev, or built and hosted by YOU.
//
//   tabvis --serve --dev  → the Python server starts `npm run dev` and reverse-proxies the console.
//   npm run dev           → Vite dev server (HMR); API calls proxied to a running tabvis server.
//   npm run build         → typecheck + a standard bundle in web/dist/ for you to host externally
//                           (point it at the tabvis API, same-origin or via your own reverse proxy).
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
  plugins: [react()],
  // Standard build -> web/dist/ (default). Host it yourself; it is not bundled into the package.
  server: {
    proxy,
    // Under `tabvis --serve --dev` the page is reverse-proxied from :8765, but the HMR websocket must
    // still reach Vite directly on :5173 (the Python side proxies only plain HTTP). Harmless when Vite
    // is opened directly on :5173 too. Override the port with TABVIS_WEB_DEV_PORT if you change it.
    hmr: { clientPort: Number(process.env.TABVIS_WEB_DEV_PORT) || 5173 },
  },
})
