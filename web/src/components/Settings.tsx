import { useEffect, useState } from 'react'
import type { ConfigResponse } from '../types'

const truthy = (v: unknown) => ['1', 'true', 'on', 'yes'].includes(String(v).toLowerCase())

export function Settings({ open, onSaved }: { open: boolean; onSaved?: () => void }) {
  const [spec, setSpec] = useState<ConfigResponse | null>(null)
  const [vals, setVals] = useState<Record<string, string>>({})
  const [base, setBase] = useState<Record<string, string>>({}) // values as loaded — anything equal is UNCHANGED
  const [err, setErr] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [saving, setSav] = useState(false)

  // Load the spec whenever the panel opens, so it reflects the server's real current values.
  useEffect(() => {
    if (!open) return
    fetch('/config')
      .then((r) => r.json())
      .then((d: ConfigResponse) => {
        setSpec(d)
        const v: Record<string, string> = {}
        // Secrets are write-only: start blank. Blank on save == "leave unchanged".
        for (const s of d.settings) v[s.key] = s.kind === 'secret' ? '' : (s.value ?? '')
        setVals(v)
        setBase(v)
      })
      .catch(() => setErr('could not load settings'))
  }, [open])

  if (!open) return null
  if (!spec)
    return (
      <div className="card full">
        <h2>Settings</h2>
        <div className="empty">Loading…</div>
      </div>
    )

  const set = (k: string, v: string) => {
    setVals((o) => ({ ...o, [k]: v }))
    setSaved(false)
  }

  const save = async (e: React.FormEvent) => {
    e.preventDefault()
    setSav(true)
    setErr(null)
    try {
      // Send ONLY what actually changed. Submitting every field would silently pin settings the
      // user never touched — e.g. saving an API key would write out the *displayed* default for
      // Headless, turning "unset, follow the default" into "explicitly set" and freezing it.
      const changed: Record<string, string> = {}
      for (const k of Object.keys(vals)) {
        if (vals[k] !== base[k]) changed[k] = vals[k]
      }
      if (Object.keys(changed).length === 0) {
        setSaved(true)
        setSav(false)
        return
      }
      const res = await fetch('/config', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ values: changed }),
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`)
      setSaved(true)
      // re-read so a just-saved secret flips to "set", and clear the typed value
      const fresh: ConfigResponse = await fetch('/config').then((r) => r.json())
      setSpec(fresh)
      const v: Record<string, string> = {}
      for (const s of fresh.settings) v[s.key] = s.kind === 'secret' ? '' : (s.value ?? '')
      setVals(v)
      setBase(v)
      onSaved?.()
    } catch (e2: any) {
      setErr(e2.message)
    }
    setSav(false)
  }

  const groups = [...new Set(spec.settings.map((s) => s.group))]
  return (
    <form className="card full" onSubmit={save}>
      <h2>Settings</h2>
      <div className="body">
        {!spec.writable && (
          <div className="ro">
            Read-only — config changes are accepted from <b>localhost only</b>, because this server has no
            authentication. Set <code>TABVIS_SERVER_ALLOW_REMOTE_CONFIG=1</code> to override.
          </div>
        )}

        {groups.map((g) => (
          <div className="grp" key={g}>
            <h3>{g}</h3>
            {spec.settings
              .filter((s) => s.group === g)
              .map((s) => (
                <div className="fld" key={s.key}>
                  <div className="lbl">
                    <label htmlFor={s.key}>{s.label}</label>
                    <span className="key">{s.key}</span>
                    {s.kind === 'secret' && s.set && <span className="isset">set {s.hint}</span>}
                  </div>
                  {s.kind === 'bool' ? (
                    <div className="sw">
                      <input
                        id={s.key}
                        type="checkbox"
                        disabled={!spec.writable}
                        checked={truthy(vals[s.key])}
                        onChange={(e) => set(s.key, e.target.checked ? '1' : '0')}
                      />
                      <span className="hint">{truthy(vals[s.key]) ? 'on' : 'off'}</span>
                    </div>
                  ) : (
                    <input
                      id={s.key}
                      disabled={!spec.writable}
                      type={s.kind === 'secret' ? 'password' : s.kind === 'number' ? 'number' : 'text'}
                      value={vals[s.key] ?? ''}
                      placeholder={s.kind === 'secret' && s.set ? 'unchanged — type to replace' : s.placeholder}
                      onChange={(e) => set(s.key, e.target.value)}
                    />
                  )}
                  {s.help && <p className="hlp">{s.help}</p>}
                </div>
              ))}
          </div>
        ))}

        <div className="actions">
          <button className="primary" type="submit" disabled={!spec.writable || saving}>
            {saving ? 'Saving…' : 'Save & apply'}
          </button>
          {saved && <span className="saved">✓ applied live — no restart needed</span>}
          <span className="spacer"></span>
          <span className="hint">writes {spec.env_file}</span>
        </div>
        {err && <div className="err">{err}</div>}
      </div>
    </form>
  )
}
