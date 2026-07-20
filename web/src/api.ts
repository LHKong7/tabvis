import type {
  AgentRecord,
  AgentSummary,
  BrowserView,
  ConfigResponse,
  DriversResponse,
  Health,
  Workspace,
} from './types'

// Same-origin JSON/SSE client for the tabvis server. In production the console is served from `/`
// by the server itself, so no base URL and no CORS; in dev Vite proxies these paths (vite.config.ts).

interface Result {
  ok: boolean
  body: any
}

const asResult = async (r: Response): Promise<Result> => ({ ok: r.ok, body: await r.json() })

export const api = {
  health: (): Promise<Health> => fetch('/health').then((r) => r.json()),
  list: (): Promise<{ agents: AgentSummary[] }> => fetch('/agents').then((r) => r.json()),
  get: (id: string): Promise<AgentRecord | null> =>
    fetch(`/agents/${id}`).then((r) => (r.ok ? r.json() : null)),
  browser: (id: string): Promise<BrowserView | null> =>
    fetch(`/agents/${id}/browser`).then((r) => (r.ok ? r.json() : null)),
  cancel: (id: string): Promise<Result> => fetch(`/agents/${id}/cancel`, { method: 'POST' }).then(asResult),
  quit: (id: string): Promise<Result> => fetch(`/agents/${id}/quit`, { method: 'POST' }).then(asResult),
  config: (): Promise<ConfigResponse> => fetch('/config').then((r) => r.json()),
  saveConfig: (values: Record<string, string>): Promise<Result> =>
    fetch('/config', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ values }),
    }).then(asResult),
  browsers: (): Promise<{ browsers: Workspace[] }> => fetch('/browsers').then((r) => r.json()),
  drivers: (): Promise<DriversResponse> => fetch('/browsers/drivers').then((r) => r.json()),
  closeBrowser: (body: Record<string, unknown>): Promise<Result> =>
    fetch('/browsers/close', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(asResult),
}

export interface RunFrame {
  event: string
  data: any
}

export class RunError extends Error {
  status?: number
  held_by?: string
}

// POST /agent streams SSE. EventSource is GET-only, so we read the body stream and parse frames.
export async function runAgent(
  body: Record<string, unknown>,
  onFrame: (f: RunFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/agent', {
    method: 'POST',
    signal,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    const err = new RunError(e.error || `HTTP ${res.status}`)
    err.status = res.status
    err.held_by = e.held_by
    throw err
  }
  onFrame({ event: '_id', data: { agent_id: res.headers.get('X-Agent-Id') } })

  const reader = res.body!.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    // sse-starlette terminates lines with CRLF, so frames are separated by \r\n\r\n.
    // Splitting on '\n\n' alone matches nothing and you silently get an empty stream.
    const parts = buf.split(/\r?\n\r?\n/)
    buf = parts.pop() ?? '' // keep the incomplete tail
    for (const part of parts) {
      let ev = 'message'
      let data = ''
      for (const raw of part.split(/\r?\n/)) {
        const line = raw.trimEnd()
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
        // lines starting with ':' are keep-alive pings — ignore
      }
      if (!data) continue
      try {
        onFrame({ event: ev, data: JSON.parse(data) })
      } catch {
        /* skip bad frame */
      }
    }
  }
}


// --- driver install progress (SSE over POST, read like runAgent) ---

export interface DriverInstallResult {
  ok: boolean
  browser: string
  installed: boolean
  message: string
}

// Stream `playwright install <browser>`: onEvent('progress'|'result'|'done', data) fires per SSE
// frame; resolves with the final result (or null if none arrived).
export async function installDriverStream(
  browser: string,
  onEvent: (event: string, data: any) => void,
): Promise<DriverInstallResult | null> {
  const res = await fetch('/browsers/install', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ browser }),
  })
  if (!res.ok) {
    const e = await res.json().catch(() => ({}))
    throw new Error(e.error || `HTTP ${res.status}`)
  }
  const reader = res.body!.getReader()
  const dec = new TextDecoder()
  let buf = ''
  let result: DriverInstallResult | null = null
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const parts = buf.split(/\r?\n\r?\n/)
    buf = parts.pop() ?? ''
    for (const part of parts) {
      let ev = 'message'
      let data = ''
      for (const raw of part.split(/\r?\n/)) {
        const line = raw.trimEnd()
        if (line.startsWith('event:')) ev = line.slice(6).trim()
        else if (line.startsWith('data:')) data += line.slice(5).trim()
      }
      if (!data) continue
      try {
        const d = JSON.parse(data)
        if (ev === 'result') result = d
        onEvent(ev, d)
      } catch {
        /* skip bad frame */
      }
    }
  }
  return result
}
