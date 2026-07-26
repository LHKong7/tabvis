import type {
  AgentRecord,
  AgentSummary,
  BrowserView,
  ConfigResponse,
  DriversResponse,
  Health,
  InteractionRecord,
  ScheduledTask,
  Workspace,
} from './types'

// Same-origin JSON/SSE client for the tabvis server. In production the console is served from `/`
// by the server itself, so no base URL and no CORS; in dev Vite proxies these paths (vite.config.ts).

interface Result {
  ok: boolean
  body: any
}

const asResult = async (r: Response): Promise<Result> => ({ ok: r.ok, body: await r.json() })

export function apiErrorMessage(body: any, fallback: string): string {
  const error = body?.error
  if (typeof error === 'string' && error.trim()) return error
  if (error && typeof error === 'object') {
    const message = typeof error.message === 'string' ? error.message : ''
    const code = typeof error.code === 'string' ? error.code : ''
    const trace = typeof error.trace_id === 'string' ? ` · trace ${error.trace_id}` : ''
    if (message) return `${code ? `${code}: ` : ''}${message}${trace}`
    if (code) return `${code}${trace}`
  }
  if (typeof body?.message === 'string' && body.message.trim()) return body.message
  return fallback
}

export const api = {
  health: (): Promise<Health> => fetch('/health').then((r) => r.json()),
  list: (): Promise<{ agents: AgentSummary[] }> => fetch('/agents').then((r) => r.json()),
  get: (id: string): Promise<AgentRecord | null> =>
    fetch(`/agents/${id}`).then((r) => (r.ok ? r.json() : null)),
  events: async (id: string): Promise<RunFrame[]> => {
    const response = await fetch(`/agents/${id}/events`)
    if (!response.ok) return []
    const frames: RunFrame[] = []
    await readSse(response, (frame) => frames.push(frame))
    return frames
  },
  browser: (id: string): Promise<BrowserView | null> =>
    fetch(`/agents/${id}/browser`).then((r) => (r.ok ? r.json() : null)),
  interactions: (id: string): Promise<{ interactions: InteractionRecord[] }> =>
    fetch(`/agents/${id}/interactions`).then((r) =>
      r.ok ? r.json() : { interactions: [] },
    ),
  respondInteraction: (
    agentId: string,
    interactionId: string,
    answers: Record<string, unknown>,
  ): Promise<Result> =>
    fetch(`/agents/${agentId}/interactions/${interactionId}/responses`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ answers }),
    }).then(asResult),
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
  scheduledTasks: (): Promise<{ scheduled_tasks: ScheduledTask[]; count: number }> =>
    fetch('/v1/scheduled-tasks').then((r) =>
      r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`)),
    ),
  createScheduledTask: (body: Record<string, unknown>): Promise<Result> =>
    fetch('/v1/scheduled-tasks', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(asResult),
  updateScheduledTask: (
    scheduledTaskId: string,
    body: Record<string, unknown>,
  ): Promise<Result> =>
    fetch(`/v1/scheduled-tasks/${scheduledTaskId}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(asResult),
  deleteScheduledTask: (scheduledTaskId: string): Promise<Result> =>
    fetch(`/v1/scheduled-tasks/${scheduledTaskId}`, { method: 'DELETE' }).then(asResult),
  runScheduledTask: (scheduledTaskId: string): Promise<Result> =>
    fetch(`/v1/scheduled-tasks/${scheduledTaskId}/run`, { method: 'POST' }).then(asResult),
}

export interface RunFrame {
  event: string
  data: any
}

export class RunError extends Error {
  status?: number
  held_by?: string
}

async function readSse(res: Response, onFrame: (frame: RunFrame) => void): Promise<void> {
  if (!res.body) return
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const emit = (part: string) => {
    let event = 'message'
    const data: string[] = []
    for (const raw of part.split(/\r?\n/)) {
      const line = raw.trimEnd()
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
    }
    if (data.length === 0) return
    try {
      onFrame({ event, data: JSON.parse(data.join('\n')) })
    } catch {
      /* skip malformed or keep-alive frames */
    }
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const parts = buffer.split(/\r?\n\r?\n/)
    buffer = parts.pop() ?? ''
    parts.forEach(emit)
  }
  buffer += decoder.decode()
  if (buffer.trim()) emit(buffer)
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
    const err = new RunError(apiErrorMessage(e, `HTTP ${res.status}`))
    err.status = res.status
    err.held_by = e.held_by
    throw err
  }
  onFrame({ event: '_id', data: { agent_id: res.headers.get('X-Agent-Id') } })
  await readSse(res, onFrame)
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
    throw new Error(apiErrorMessage(e, `HTTP ${res.status}`))
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
