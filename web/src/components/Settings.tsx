import { useEffect, useState } from 'react'
import type { ConfigResponse, HealthConfig } from '../types'

const truthy = (v: unknown) => ['1', 'true', 'on', 'yes'].includes(String(v).toLowerCase())

// Which browser engine runs in which "mode", so the console can tell (say) a CDP-attach engine from
// a native launch even before the health catalog has loaded.
const MODE_BY_ENGINE: Record<string, string> = {
  chromium: 'launch', chrome: 'launch', msedge: 'launch', brave: 'launch', vivaldi: 'launch',
  opera: 'launch', firefox: 'launch', webkit: 'launch',
  cloak: 'plugin', camoufox: 'plugin',
  cdp: 'cdp', steel: 'cdp', adspower: 'cdp', gologin: 'cdp', multilogin: 'cdp', octo: 'cdp',
  dolphin: 'cdp', kameleo: 'cdp',
  connect: 'connect', browserless: 'connect', browserbase: 'connect',
}
// Native-launch-only (a stealth engine drives its own binary; remote engines launch nothing).
const NATIVE_LAUNCH_ONLY = new Set(['TABVIS_BROWSER_CHANNEL', 'TABVIS_BROWSER_EXECUTABLE_PATH'])
// Any locally-launched browser (native or stealth), but not a remote/attach engine.
const LOCAL_LAUNCH = new Set([
  'TABVIS_BROWSER_USER_DATA_DIR', 'TABVIS_BROWSER_HEADLESS', 'TABVIS_BROWSER_ARGS', 'TABVIS_BROWSER_VIEWPORT',
])

type Applicability = { applies: boolean; note?: string; required?: boolean }

// Does a setting apply to the active engine's mode? Only ONE of the two situations (a launched
// browser vs. a remote CDP/connect attach) is ever in effect, so the other side's fields are dimmed.
function applicability(key: string, group: string, mode: string): Applicability {
  if (group === 'Stealth')
    return mode === 'plugin'
      ? { applies: true }
      : { applies: false, note: 'only used by stealth engines (cloak / camoufox)' }
  if (key === 'TABVIS_BROWSER_CDP_ENDPOINT')
    return mode === 'cdp'
      ? { applies: true, required: true, note: 'required for the current CDP engine' }
      : { applies: false, note: 'only used by CDP-attach engines' }
  if (key === 'TABVIS_BROWSER_WS_ENDPOINT')
    return mode === 'connect'
      ? { applies: true, required: true, note: 'required for the current connect engine' }
      : { applies: false, note: 'only used by connect / Playwright-server engines' }
  if (NATIVE_LAUNCH_ONLY.has(key)) {
    if (mode === 'launch') return { applies: true }
    if (mode === 'plugin') return { applies: false, note: 'ignored — the stealth engine drives its own binary' }
    return { applies: false, note: 'not used — the current engine attaches to a remote browser' }
  }
  if (LOCAL_LAUNCH.has(key))
    return mode === 'launch' || mode === 'plugin'
      ? { applies: true }
      : { applies: false, note: 'not used — the remote browser owns the profile / window' }
  return { applies: true }
}

export function Settings({
  open,
  config,
  onSaved,
}: {
  open: boolean
  config?: HealthConfig
  onSaved?: () => void
}) {
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

  // The active browser engine (live from the form, so dimming reacts as you change it) and its mode.
  const engine = (vals['TABVIS_BROWSER_ENGINE'] || config?.browser_engine || 'chromium').trim().toLowerCase()
  const mode =
    (config?.browser_engines || []).find((e) => e.key === engine)?.mode || MODE_BY_ENGINE[engine] || 'launch'

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
        <p className="hint" style={{ marginBottom: '10px' }}>
          Browser engine: <b>{engine}</b> ({mode}). Settings the active engine doesn't use are dimmed.
        </p>

        {groups.map((g) => (
          <div className="grp" key={g}>
            <h3>{g}</h3>
            {spec.settings
              .filter((s) => s.group === g)
              .map((s) => {
                const ap = applicability(s.key, s.group, mode)
                return (
                  <div className={ap.applies ? 'fld' : 'fld na'} key={s.key}>
                    <div className="lbl">
                      <label htmlFor={s.key}>{s.label}</label>
                      <span className="key">{s.key}</span>
                      {s.kind === 'secret' && s.set && <span className="isset">set {s.hint}</span>}
                      {ap.note && <span className={ap.required ? 'req-note' : 'na-note'}>· {ap.note}</span>}
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
                )
              })}
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
