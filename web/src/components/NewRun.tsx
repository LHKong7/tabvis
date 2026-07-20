import { useState } from 'react'
import type { AgentSummary, HealthConfig } from '../types'
import { api } from '../api'

interface Props {
  onLaunched: (body: Record<string, unknown>, setErr: (e: string) => void) => void
  busy: boolean
  ready: boolean
  agents: AgentSummary[]
  config?: HealthConfig
  runOn: string // '' = new agent; else an agent_id to continue
  onRunOn: (id: string) => void
  onEngineChanged?: () => void
}

const MODE_LABEL: Record<string, string> = {
  launch: 'Native launch',
  plugin: 'Stealth',
  cdp: 'Attach over CDP',
  connect: 'Playwright server',
}

export function NewRun({ onLaunched, busy, ready, agents, config, runOn, onRunOn, onEngineChanged }: Props) {
  const [prompt, setPrompt] = useState('open example.com and tell me the heading')
  const [model, setModel] = useState('')
  const [profile, setProfile] = useState('')
  const [maxTurns, setMaxTurns] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [engineBusy, setEngineBusy] = useState(false)

  const continuing = !!runOn
  const engine = config?.browser_engine || 'chromium'
  const engines = config?.browser_engines || []

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    setErr(null)
    if (!prompt.trim()) return
    const body: Record<string, unknown> = { prompt }
    if (model.trim()) body.model = model.trim()
    if (maxTurns) body.max_turns = Number(maxTurns)
    if (continuing) {
      body.agent_id = runOn // same session + browser + profile
    } else if (profile) {
      body.profile = profile
    }
    onLaunched(body, setErr)
  }

  // Choosing a browser engine sets the global TABVIS_BROWSER_ENGINE (applies to the next NEW agent).
  const changeEngine = async (next: string) => {
    if (next === engine) return
    setEngineBusy(true)
    const { ok, body } = await api.saveConfig({ TABVIS_BROWSER_ENGINE: next })
    setEngineBusy(false)
    if (!ok) return setErr(body.error || 'could not switch browser')
    onEngineChanged?.()
  }

  return (
    <form className="card" onSubmit={submit}>
      <h2>New run</h2>
      <div className="body">
        <label>Prompt</label>
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="what should the agent do?"
        />

        {/* Agent selection: a fresh agent, or continue an existing one's session + browser. */}
        <div className="mt12">
          <label>Agent</label>
          <select value={runOn} onChange={(e) => onRunOn(e.target.value)}>
            <option value="">New agent (fresh session + browser)</option>
            {agents.map((a) => (
              <option key={a.agent_id} value={a.agent_id}>
                Continue {a.agent_id} · {a.status} · {a.prompt.slice(0, 40)}
              </option>
            ))}
          </select>
          {continuing && (
            <p className="hint">Reuses this agent's session, browser &amp; profile — engine and profile are fixed.</p>
          )}
        </div>

        {/* Browser choice + profile only matter for a NEW agent. */}
        {!continuing && (
          <div className="row mt12">
            <div>
              <label>Browser</label>
              <select value={engine} disabled={engineBusy} onChange={(e) => changeEngine(e.target.value)}>
                {engines.length === 0 ? (
                  <option value={engine}>{engine}</option>
                ) : (
                  ['launch', 'plugin', 'cdp', 'connect'].map((m) => {
                    const items = engines.filter((x) => x.mode === m)
                    if (!items.length) return null
                    return (
                      <optgroup key={m} label={MODE_LABEL[m] || m}>
                        {items.map((x) => (
                          <option key={x.key} value={x.key}>
                            {x.label}
                            {x.stealth ? ' · stealth' : ''}
                          </option>
                        ))}
                      </optgroup>
                    )
                  })
                )}
              </select>
            </div>
            <div>
              <label>Profile</label>
              <select value={profile} onChange={(e) => setProfile(e.target.value)}>
                <option value="">isolated (parallel)</option>
                <option value="default">default (logged-in, exclusive)</option>
              </select>
            </div>
          </div>
        )}

        <div className="row mt12">
          <div>
            <label>Model</label>
            <input value={model} placeholder="default" onChange={(e) => setModel(e.target.value)} />
          </div>
          <div>
            <label>Max turns</label>
            <input
              type="number"
              min="1"
              value={maxTurns}
              placeholder="∞"
              onChange={(e) => setMaxTurns(e.target.value)}
            />
          </div>
        </div>

        <div className="actions">
          <button
            className="primary"
            type="submit"
            disabled={busy || !prompt.trim() || !ready}
            title={ready ? '' : 'server has no model endpoint configured'}
          >
            {busy ? 'Starting…' : continuing ? 'Send to agent' : 'Run agent'}
          </button>
          <span className="hint">
            {!ready
              ? 'model endpoint not configured'
              : continuing
                ? 'continues the selected session'
                : profile === 'default'
                  ? 'uses your logged-in browser'
                  : `${engine} · its own browser`}
          </span>
        </div>
        {err && <div className="err">{err}</div>}
      </div>
    </form>
  )
}
