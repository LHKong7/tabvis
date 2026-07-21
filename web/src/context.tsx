import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, runAgent, RunError } from './api'
import { summarize } from './format'
import type { AgentSummary, Frame, Health } from './types'

// App-wide shared state: the polled fleet/session list, and the live run lifecycle (which persists as
// you navigate between routes). Per-session detail polling lives in the SessionDetail page instead.
interface AppValue {
  health: Health | null
  agents: AgentSummary[]
  ready: boolean
  frames: Frame[]
  busy: boolean
  cancelling: boolean
  runOn: string // '' = new agent; else an agent_id to continue
  streamFor: string | null // agent_id whose live stream `frames` holds
  setRunOn: (id: string) => void
  launch: (body: Record<string, unknown>, setErr: (e: string) => void) => void
  cancel: (id: string) => Promise<void>
  quit: (id: string) => Promise<void>
  refreshHealth: () => Promise<void>
}

const Ctx = createContext<AppValue | null>(null)

export function useApp(): AppValue {
  const v = useContext(Ctx)
  if (!v) throw new Error('useApp must be used inside <AppProvider>')
  return v
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [frames, setFrames] = useState<Frame[]>([])
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancel] = useState(false)
  const [runOn, setRunOn] = useState('')
  const [streamFor, setStreamFor] = useState<string | null>(null)
  const streamRef = useRef<string | null>(null)
  const navigate = useNavigate()

  // Poll the fleet + session list.
  useEffect(() => {
    let stop = false
    const tick = async () => {
      try {
        const [hp, ls] = await Promise.all([api.health(), api.list()])
        if (stop) return
        setHealth(hp)
        setAgents(ls.agents || [])
      } catch {
        /* server momentarily down — keep last */
      }
    }
    tick()
    const t = setInterval(tick, 1500)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [])

  const push = useCallback((event: string, data: any) => {
    const out = summarize(event, data)
    if (out == null) return
    const text = typeof out === 'string' ? out : out.text
    const cls = typeof out === 'string' ? event : out.cls
    setFrames((f) => [...f, { event, cls, text }])
  }, [])

  const launch = useCallback(
    (body: Record<string, unknown>, setErr: (e: string) => void) => {
      setBusy(true)
      setFrames([])
      const continuing = !!body.agent_id
      runAgent(body, ({ event, data }) => {
        if (event === '_id' || event === 'agent') {
          const id = data.agent_id
          if (id && streamRef.current !== id) {
            streamRef.current = id
            setStreamFor(id)
            if (!continuing) setRunOn(id) // chat continuation
            navigate(`/sessions/${id}`)
          }
        }
        push(event, data)
      })
        .catch((e: RunError) => {
          const extra = e.status === 409 ? ` (held by ${e.held_by})` : ''
          setErr(`${e.message}${extra}`)
        })
        .finally(() => setBusy(false))
    },
    [push, navigate],
  )

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health())
    } catch {
      /* ignore */
    }
  }, [])

  const cancel = useCallback(
    async (id: string) => {
      setCancel(true)
      const { ok, body } = await api.cancel(id)
      if (!ok) push('error', { message: body.error })
      setCancel(false)
    },
    [push],
  )

  const quit = useCallback(
    async (id: string) => {
      setCancel(true)
      const { ok, body } = await api.quit(id)
      if (!ok) push('error', { message: body.error })
      setCancel(false)
    },
    [push],
  )

  const value: AppValue = {
    health,
    agents,
    ready: health?.config?.ready !== false,
    frames,
    busy,
    cancelling,
    runOn,
    streamFor,
    setRunOn,
    launch,
    cancel,
    quit,
    refreshHealth,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
