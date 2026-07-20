import { useState } from 'react'

interface Props {
  onLaunched: (body: Record<string, unknown>, setErr: (e: string) => void) => void
  busy: boolean
  ready: boolean
}

export function NewRun({ onLaunched, busy, ready }: Props) {
  const [prompt, setPrompt] = useState('open example.com and tell me the heading')
  const [model, setModel] = useState('')
  const [profile, setProfile] = useState('')
  const [maxTurns, setMaxTurns] = useState('')
  const [err, setErr] = useState<string | null>(null)

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    setErr(null)
    if (!prompt.trim()) return
    const body: Record<string, unknown> = { prompt }
    if (model.trim()) body.model = model.trim()
    if (profile) body.profile = profile
    if (maxTurns) body.max_turns = Number(maxTurns)
    onLaunched(body, setErr)
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

        <div className="row mt12">
          <div>
            <label>Browser profile</label>
            <select value={profile} onChange={(e) => setProfile(e.target.value)}>
              <option value="">isolated (parallel)</option>
              <option value="default">default (logged-in, exclusive)</option>
            </select>
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

        <div className="mt12">
          <label>Model</label>
          <input value={model} placeholder="default" onChange={(e) => setModel(e.target.value)} />
        </div>

        <div className="actions">
          <button
            className="primary"
            type="submit"
            disabled={busy || !prompt.trim() || !ready}
            title={ready ? '' : 'server has no model endpoint configured'}
          >
            {busy ? 'Starting…' : 'Run agent'}
          </button>
          <span className="hint">
            {!ready
              ? 'model endpoint not configured'
              : profile === 'default'
                ? 'uses your logged-in browser'
                : 'gets its own browser'}
          </span>
        </div>
        {err && <div className="err">{err}</div>}
      </div>
    </form>
  )
}
