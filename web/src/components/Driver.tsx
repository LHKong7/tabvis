import { useCallback, useEffect, useState } from 'react'
import type { DriverInfo, EngineInfo, HealthConfig, Setting, Workspace } from '../types'
import { api, installDriverStream } from '../api'
import { Code } from './Code'

const MODE_LABEL: Record<string, string> = {
  launch: 'Native launch',
  plugin: 'Stealth (plugin SDK)',
  cdp: 'Attach over CDP',
  connect: 'Attach to Playwright server',
}

interface Props {
  open: boolean
  config?: HealthConfig
  onChanged?: () => void
}

// The browser-driver page: pick the engine (stock Chromium vs CloakBrowser stealth), tune the stealth
// knobs, and see/close the live browser workspaces. The engine is just TABVIS_BROWSER_ENGINE under the
// hood, written via POST /config, so it takes effect on the NEXT run — hence the "close the current
// browser to start fresh" loop that the workspace list below closes.
export function Driver({ open, config, onChanged }: Props) {
  const [cfg, setCfg] = useState<{ writable?: boolean; settings?: Setting[] } | null>(null) // GET /config
  const [sv, setSv] = useState<Record<string, string>>({}) // stealth form values
  const [sbase, setSbase] = useState<Record<string, string>>({}) // as-loaded baseline (send only what changed)
  const [ws, setWs] = useState<Workspace[] | null>(null) // live workspaces (GET /browsers)
  const [err, setErr] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null) // transient success line
  const [busy, setBusy] = useState(false)
  const [drivers, setDrivers] = useState<DriverInfo[] | null>(null) // GET /browsers/drivers
  const [installing, setInstalling] = useState<string | null>(null) // browser being downloaded
  const [installProgress, setInstallProgress] = useState<string | null>(null) // latest SSE line
  const [driverNote, setDriverNote] = useState<string | null>(null)

  const engine = config?.browser_engine || 'chromium'
  const writable = cfg?.writable !== false

  const loadCfg = useCallback(() => {
    api
      .config()
      .then((d) => {
        setCfg(d)
        const v: Record<string, string> = {}
        for (const s of d.settings) if (s.group === 'Stealth') v[s.key] = s.kind === 'secret' ? '' : (s.value ?? '')
        setSv(v)
        setSbase(v)
      })
      .catch(() => setErr('could not load driver config'))
  }, [])

  const loadDrivers = useCallback(() => {
    api
      .drivers()
      .then((d) => setDrivers(d.drivers || []))
      .catch(() => {})
  }, [])

  useEffect(() => {
    if (open) {
      loadCfg()
      loadDrivers()
    }
  }, [open, loadCfg, loadDrivers])

  // Poll the live workspaces only while the panel is open.
  useEffect(() => {
    if (!open) return
    let stop = false
    const tick = () =>
      api
        .browsers()
        .then((d) => {
          if (!stop) setWs(d.browsers || [])
        })
        .catch(() => {})
    tick()
    const t = setInterval(tick, 2000)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [open])

  if (!open) return null

  const flash = (m: string) => {
    setNote(m)
    setTimeout(() => setNote(null), 3000)
  }
  const isOn = (v: unknown) => ['1', 'true', 'on', 'yes'].includes(String(v).toLowerCase())

  const switchEngine = async (next: string) => {
    if (next === engine || busy) return
    setBusy(true)
    setErr(null)
    const { ok, body } = await api.saveConfig({ TABVIS_BROWSER_ENGINE: next })
    setBusy(false)
    if (!ok) return setErr(body.error || 'could not switch engine')
    flash(`Driver set to ${next}. It applies to the next run — close the current browser below to start fresh on it.`)
    onChanged?.() // refresh health so readiness + the profile dir update
    loadCfg()
  }

  const saveStealth = async () => {
    const changed: Record<string, string> = {}
    for (const k of Object.keys(sv)) if (sv[k] !== sbase[k]) changed[k] = sv[k]
    if (!Object.keys(changed).length) return flash('No changes to save.')
    setBusy(true)
    setErr(null)
    const { ok, body } = await api.saveConfig(changed)
    setBusy(false)
    if (!ok) return setErr(body.error || 'could not save')
    flash('Stealth options applied — effective on the next run.')
    onChanged?.()
    loadCfg()
  }

  const closeWs = async (w: Workspace) => {
    setErr(null)
    const { ok, body } = await api.closeBrowser({ user_data_dir: w.user_data_dir, profile: w.profile })
    if (!ok) return setErr(body.error || 'could not close browser')
    api
      .browsers()
      .then((d) => setWs(d.browsers || []))
      .catch(() => {})
  }

  // Download a driver by catalog key via the server: Playwright engines (chromium/firefox/webkit/
  // chrome/msedge) go through `playwright install <key>`, stealth engines (cloak/camoufox) through
  // `uv pip install <pkg>`. Progress lines stream over SSE as it downloads.
  const installDriver = async (browser: string) => {
    setInstalling(browser)
    setInstallProgress(null)
    setDriverNote(null)
    setErr(null)
    try {
      const result = await installDriverStream(browser, (event, d) => {
        if (event === 'progress') setInstallProgress(d.text)
      })
      setInstalling(null)
      setInstallProgress(null)
      if (result?.ok) {
        setDriverNote(`${browser} installed.`)
        setTimeout(() => setDriverNote(null), 4000)
        loadDrivers()
        onChanged?.()
      } else {
        setErr(result?.message || `could not install ${browser}`)
      }
    } catch (e: any) {
      setInstalling(null)
      setInstallProgress(null)
      setErr(e.message || `could not install ${browser}`)
    }
  }

  const stealth = (cfg?.settings || []).filter((s) => s.group === 'Stealth')

  return (
    <div className="card full">
      <h2>Browser driver</h2>
      <div className="body">
        {!writable && (
          <div className="ro">
            Read-only — the driver can only be changed from <b>localhost</b> (this server has no auth). Set{' '}
            <code>TABVIS_SERVER_ALLOW_REMOTE_CONFIG=1</code> to override.
          </div>
        )}

        {(() => {
          const engines = config?.browser_engines || []
          const cur: Partial<EngineInfo> = engines.find((e) => e.key === engine) || {}
          const groups = ['launch', 'plugin', 'cdp', 'connect']
          const pkgReady = config?.engine_package_ready !== false
          const epReady = config?.engine_endpoint_ready !== false
          return (
            <>
              <label htmlFor="drv-engine">
                Engine <span className="hint">— which browser the agent drives (TABVIS_BROWSER_ENGINE)</span>
              </label>
              <select
                id="drv-engine"
                disabled={!writable || busy}
                value={engine}
                onChange={(e) => switchEngine(e.target.value)}
              >
                {groups.map((g) => {
                  const items = engines.filter((e) => e.mode === g)
                  if (!items.length) return null
                  return (
                    <optgroup key={g} label={MODE_LABEL[g] || g}>
                      {items.map((e) => (
                        <option key={e.key} value={e.key}>
                          {e.label}
                          {e.stealth ? ' · stealth' : ''} ({e.kernel})
                        </option>
                      ))}
                    </optgroup>
                  )
                })}
              </select>
              {cur.notes && <p className="hlp">{cur.notes}</p>}
              {note && <p className="saved mt12">✓ {note}</p>}

              {cur.requires && !pkgReady && (
                <div className="banner mt12">
                  <span className="ico">⚠</span>
                  <div>
                    <b>
                      {cur.label} is selected, but the {cur.requires} package isn't installed.
                    </b>
                    <p>Runs refuse to launch until you install the optional extra, then retry:</p>
                    <Code>{`uv sync --extra ${cur.requires === 'cloakbrowser' ? 'cloak' : cur.requires}`}</Code>
                  </div>
                </div>
              )}
              {cur.mode === 'cdp' && !epReady && (
                <div className="banner mt12">
                  <span className="ico">⚠</span>
                  <div>
                    <b>{cur.label} attaches over CDP, but no endpoint is set.</b>
                    <p>Start the browser/profile, then set its DevTools address:</p>
                    <Code>TABVIS_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222</Code>
                  </div>
                </div>
              )}
              {cur.mode === 'connect' && !epReady && (
                <div className="banner mt12">
                  <span className="ico">⚠</span>
                  <div>
                    <b>{cur.label} attaches to a Playwright server, but no endpoint is set.</b>
                    <p>Set the connect URL (it may carry a ?token=):</p>
                    <Code>TABVIS_BROWSER_WS_ENDPOINT=wss://…</Code>
                  </div>
                </div>
              )}

              <div className="grp">
                <h3>Status</h3>
                <dl className="facts">
                  <dt>active engine</dt>
                  <dd>
                    {cur.label || engine}{' '}
                    <span className={config?.engine_ready ? 'ok' : 'bad'}>
                      {config?.engine_ready ? '· ready' : '· not ready'}
                    </span>
                  </dd>
                  <dt>kernel · driver</dt>
                  <dd>
                    {cur.kernel || config?.browser_kernel || '—'} · {cur.browser_type || config?.browser_type || '—'}
                  </dd>
                  <dt>connection</dt>
                  <dd>
                    {MODE_LABEL[cur.mode || ''] || cur.mode || config?.browser_mode || '—'}
                    {cur.stealth ? <span className="badge b-cloak"> stealth</span> : ''}
                  </dd>
                  {cur.requires && (
                    <>
                      <dt>package</dt>
                      <dd className={pkgReady ? 'ok' : 'bad'}>
                        {cur.requires} {pkgReady ? 'installed' : 'not installed'}
                      </dd>
                    </>
                  )}
                  {cur.mode === 'cdp' && (
                    <>
                      <dt>CDP endpoint</dt>
                      <dd className={config?.browser_cdp_endpoint_set ? 'ok' : 'bad'}>
                        {config?.browser_cdp_endpoint_set ? 'set' : 'not set'}
                      </dd>
                    </>
                  )}
                  {cur.mode === 'connect' && (
                    <>
                      <dt>ws endpoint</dt>
                      <dd className={config?.browser_ws_endpoint_set ? 'ok' : 'bad'}>
                        {config?.browser_ws_endpoint_set ? 'set' : 'not set'}
                      </dd>
                    </>
                  )}
                  {engine === 'cloak' && (
                    <>
                      <dt>license</dt>
                      <dd>{config?.cloak_licensed ? <span className="ok">Pro key set</span> : 'free tier'}</dd>
                    </>
                  )}
                  <dt>profile dir</dt>
                  <dd>
                    {cur.mode === 'cdp' || cur.mode === 'connect' ? (
                      <span className="hint">— remote (no local profile)</span>
                    ) : (
                      (config?.browser_profile_dir ?? '—')
                    )}
                  </dd>
                  <dt>window</dt>
                  <dd>
                    {cur.mode === 'cdp' || cur.mode === 'connect' ? (
                      <span className="hint">— controlled by the remote browser</span>
                    ) : config?.browser_headless ? (
                      'headless'
                    ) : (
                      'headed'
                    )}
                  </dd>
                </dl>
              </div>
            </>
          )
        })()}

        <div className="grp">
          <h3>Browser drivers{drivers ? ` (${drivers.length})` : ''}</h3>
          {driverNote && <p className="saved mt12">✓ {driverNote}</p>}
          {installing && (
            <p className="hint mt12">
              ⏳ downloading {installing}: {installProgress || 'starting…'}
            </p>
          )}
          {drivers == null ? (
            <div className="muted">Loading…</div>
          ) : (
            <div className="wslist">
              {drivers.map((d) => (
                <div className="ws" key={d.key}>
                  <div className="wsmain">
                    <div className="wstop">
                      <span className="wsname">{d.label}</span>
                      <span className="badge">{d.category}</span>
                      {d.installed === true && <span className="isset">installed</span>}
                      {d.installed === false && <span className="drv">not installed</span>}
                      {d.key === engine && <span className="badge b-chromium">active</span>}
                    </div>
                    <div className="wsmeta">{d.hint}</div>
                  </div>
                  {d.installable && d.installed !== true && (
                    <button
                      className="primary"
                      type="button"
                      disabled={!!installing || !writable}
                      title={writable ? 'Download this browser' : 'read-only (localhost only)'}
                      onClick={() => installDriver(d.key)}
                    >
                      {installing === d.key ? 'Downloading…' : 'Download'}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="grp">
          <h3>
            Stealth options
            {engine !== 'cloak' ? <span className="hint"> — apply when Cloak is the engine</span> : ''}
          </h3>
          {stealth.map((s) => (
            <div className="fld" key={s.key}>
              <div className="lbl">
                <label htmlFor={'drv-' + s.key}>{s.label}</label>
                <span className="key">{s.key}</span>
                {s.kind === 'secret' && s.set && <span className="isset">set {s.hint}</span>}
              </div>
              {s.kind === 'bool' ? (
                <div className="sw">
                  <input
                    id={'drv-' + s.key}
                    type="checkbox"
                    disabled={!writable}
                    checked={isOn(sv[s.key])}
                    onChange={(e) => setSv((o) => ({ ...o, [s.key]: e.target.checked ? '1' : '0' }))}
                  />
                  <span className="hint">{isOn(sv[s.key]) ? 'on' : 'off'}</span>
                </div>
              ) : s.key === 'TABVIS_BROWSER_HUMAN_PRESET' ? (
                <select
                  id={'drv-' + s.key}
                  disabled={!writable}
                  value={sv[s.key] || 'default'}
                  onChange={(e) => setSv((o) => ({ ...o, [s.key]: e.target.value }))}
                >
                  <option value="default">default</option>
                  <option value="careful">careful</option>
                </select>
              ) : (
                <input
                  id={'drv-' + s.key}
                  disabled={!writable}
                  type={s.kind === 'secret' ? 'password' : 'text'}
                  value={sv[s.key] ?? ''}
                  placeholder={s.kind === 'secret' && s.set ? 'unchanged — type to replace' : s.placeholder}
                  onChange={(e) => setSv((o) => ({ ...o, [s.key]: e.target.value }))}
                />
              )}
              {s.help && <p className="hlp">{s.help}</p>}
            </div>
          ))}
          <div className="actions">
            <button className="primary" type="button" disabled={!writable || busy} onClick={saveStealth}>
              {busy ? 'Saving…' : 'Save stealth options'}
            </button>
            <span className="hint">effective on the next run</span>
          </div>
        </div>

        <div className="grp">
          <h3>Open browsers{ws ? ` (${ws.length})` : ''}</h3>
          {ws == null ? (
            <div className="muted">Loading…</div>
          ) : ws.length === 0 ? (
            <div className="muted">No browser open right now — one launches when the next run starts.</div>
          ) : (
            <div className="wslist">
              {ws.map((w) => {
                const bi = w.browser || {}
                const eng = bi.engine || '?'
                const tabs = (w.tabs || []).length
                return (
                  <div className="ws" key={w.user_data_dir}>
                    <div className="wsmain">
                      <div className="wstop">
                        <span className="wsname">{w.profile}</span>
                        <span className={'badge ' + (eng === 'cloak' ? 'b-cloak' : 'b-chromium')}>
                          {eng}
                          {bi.stealth ? ' · stealth' : ''}
                        </span>
                        {w.busy_agent && <span className="drv">driving: {w.busy_agent}</span>}
                      </div>
                      <div className="wsmeta">
                        {bi.version ? 'v' + bi.version + ' · ' : ''}
                        {bi.headless ? 'headless' : 'headed'} · {tabs} tab{tabs === 1 ? '' : 's'} ·{' '}
                        {w.busy_agent ? 'busy' : `idle ${Math.round(w.idle_seconds || 0)}s`}
                      </div>
                    </div>
                    <button
                      className="danger"
                      type="button"
                      disabled={!!w.busy_agent}
                      title={w.busy_agent ? 'an agent is driving this browser' : 'close this browser'}
                      onClick={() => closeWs(w)}
                    >
                      Close
                    </button>
                  </div>
                )
              })}
            </div>
          )}
        </div>

        {err && <div className="err">{err}</div>}
      </div>
    </div>
  )
}
