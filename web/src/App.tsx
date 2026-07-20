import { useCallback, useEffect, useRef, useState } from 'react'
import { api, runAgent, RunError } from './api'
import { summarize } from './format'
import type { AgentRecord, AgentSummary, BrowserView, Frame, Health as HealthT } from './types'
import { Health } from './components/Health'
import { Banner } from './components/Banner'
import { Driver } from './components/Driver'
import { Settings } from './components/Settings'
import { Setup } from './components/Setup'
import { NewRun } from './components/NewRun'
import { AgentList } from './components/AgentList'
import { Stream } from './components/Stream'
import { Detail } from './components/Detail'

export function App() {
  const [health, setHealth] = useState<HealthT | null>(null)
  const [agents, setAgents] = useState<AgentSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [runOn, setRunOn] = useState<string>('') // '' = new agent; else an agent_id to continue
  const [agent, setAgent] = useState<AgentRecord | null>(null)
  const [browser, setBrowser] = useState<BrowserView | null>(null)
  const [frames, setFrames] = useState<Frame[]>([])
  const [busy, setBusy] = useState(false)
  const [cancelling, setCancel] = useState(false)
  const [setupOpen, setSetup] = useState(false)
  const [cfgOpen, setCfgOpen] = useState(false)
  const [driverOpen, setDriver] = useState(false)
  const streamFor = useRef<string | null>(null) // agent_id whose stream we're rendering

  // poll fleet + list
  useEffect(() => {
    let stop = false
    let firstLoad = true
    const tick = async () => {
      try {
        const [hp, ls] = await Promise.all([api.health(), api.list()])
        if (stop) return
        setHealth(hp)
        setAgents(ls.agents || [])
        // Nothing works without a model endpoint — show the how-to-run panel unprompted.
        if (firstLoad && hp?.config && !hp.config.ready) setCfgOpen(true)
        firstLoad = false
      } catch {
        /* server down; keep last */
      }
    }
    tick()
    const t = setInterval(tick, 1500)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [])

  // poll the selected agent + its browser
  useEffect(() => {
    if (!selected) {
      setAgent(null)
      setBrowser(null)
      return
    }
    let stop = false
    const tick = async () => {
      const [a, b] = await Promise.all([api.get(selected), api.browser(selected)])
      if (!stop) {
        setAgent(a)
        setBrowser(b)
      }
    }
    tick()
    const t = setInterval(tick, 1500)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [selected])

  const push = useCallback((event: string, data: any) => {
    const out = summarize(event, data)
    if (out == null) return // noise (empty turn, user dupe, delta)
    // A failed run reports itself via `result` with is_error — colour it as the error it is.
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
          if (id && streamFor.current !== id) {
            streamFor.current = id
            setSelected(id)
            // Chat continuation: after launching a NEW agent, aim follow-ups at it.
            if (!continuing) setRunOn(id)
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
    [push],
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

  // Quit an agent: end it AND close its bundled browser, freeing the profile. Works on a finished
  // agent too (its browser outlives the run until quit).
  const quit = useCallback(
    async (id: string) => {
      setCancel(true)
      const { ok, body } = await api.quit(id)
      if (!ok) push('error', { message: body.error })
      setCancel(false)
    },
    [push],
  )

  return (
    <>
      <header>
        <h1>tabvis</h1>
        <span className="sub">agent console</span>
        <span className="spacer"></span>
        <Health h={health} />
        <button onClick={() => setDriver((v) => !v)}>
          {driverOpen ? 'Hide driver' : `Driver · ${health?.config?.browser_engine || 'chromium'}`}
        </button>
        <button onClick={() => setCfgOpen((v) => !v)}>{cfgOpen ? 'Hide settings' : 'Settings'}</button>
        <button onClick={() => setSetup((v) => !v)}>{setupOpen ? 'Hide setup' : 'Run as a web server'}</button>
      </header>
      <main>
        <Banner config={health?.config} onConfigure={() => setCfgOpen(true)} />
        <Driver open={driverOpen} config={health?.config} onChanged={refreshHealth} />
        <Settings open={cfgOpen} config={health?.config} onSaved={refreshHealth} />
        <Setup config={health?.config} open={setupOpen} />
        <div className="stack">
          <NewRun
            onLaunched={launch}
            busy={busy}
            ready={health?.config?.ready !== false}
            agents={agents}
            config={health?.config}
            runOn={runOn}
            onRunOn={setRunOn}
            onEngineChanged={refreshHealth}
          />
          <AgentList agents={agents} selected={selected} onSelect={setSelected} />
        </div>
        <div className="stack">
          <Stream frames={frames} />
          <Detail
            agent={agent}
            browser={browser}
            onCancel={cancel}
            onQuit={quit}
            cancelling={cancelling}
            onContinue={setRunOn}
          />
        </div>
      </main>
    </>
  )
}
