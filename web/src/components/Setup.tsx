import type { HealthConfig } from '../types'
import { Code } from './Code'

export function Setup({ config, open }: { config?: HealthConfig; open: boolean }) {
  const origin = window.location.origin
  if (!open) return null
  return (
    <div className="card full">
      <h2>Run as a web server</h2>
      <div className="body">
        <div className="steps">
          <div className="step">
            <h3>Install</h3>
            <p>Once, from the repo root. The second line fetches the Chromium the browser tools drive.</p>
            <Code>{`uv sync\nuv run playwright install chromium`}</Code>
          </div>

          <div className="step">
            <h3>Configure the model endpoint</h3>
            <p>
              Create a <code>.env</code> in the directory you launch from. Both are required — there is no
              default endpoint, and it must be spelled <code>TABVIS_*</code> (a plain{' '}
              <code>ANTHROPIC_API_KEY</code> is deleted at startup).
            </p>
            <Code>{`TABVIS_BASE_URL=https://api.anthropic.com\nTABVIS_API_KEY=sk-ant-...`}</Code>
          </div>

          <div className="step">
            <h3>Start the server</h3>
            <p>
              Serves this console at <code>/</code> and the JSON/SSE API alongside it.
            </p>
            <Code>uv run tabvis --serve</Code>
            <Code>uv run tabvis --serve --host 0.0.0.0 --port 9000</Code>
            <p className="hint" style={{ marginTop: '7px' }}>
              🔒 No authentication — anyone who can reach the port can run an agent with full shell, file and
              browser access as you. Keep it on 127.0.0.1 unless you put an auth proxy in front.
            </p>
          </div>

          <div className="step">
            <h3>Or drive it from the terminal</h3>
            <p>
              Same API this console uses. <code>-N</code> keeps the SSE stream unbuffered.
            </p>
            <Code>{`curl -N -X POST ${origin}/agent \\\n  -H 'content-type: application/json' \\\n  -d '{"prompt": "open example.com and tell me the heading"}'`}</Code>
            <Code>{`curl ${origin}/agents            # list runs\ncurl ${origin}/agents/<id>       # one record\ncurl -X POST ${origin}/agents/<id>/cancel`}</Code>
          </div>

          <div className="step">
            <h3>Useful knobs</h3>
            <Code>{`TABVIS_SERVER_PORT=9000          # or --port\nTABVIS_SERVER_MAX_AGENTS=4       # each agent = one real Chromium\nTABVIS_BROWSER_HEADLESS=0        # watch the browser drive\nTABVIS_BROWSER_EAGER=0           # don't pre-launch a browser`}</Code>
          </div>
        </div>

        <h3 className="mt12" style={{ fontSize: '12.5px' }}>
          This server
        </h3>
        <dl className="facts">
          <dt>model endpoint</dt>
          <dd className={config?.base_url ? 'ok' : 'bad'}>
            {config?.base_url ? 'configured' : 'MISSING (TABVIS_BASE_URL)'}
          </dd>
          <dt>credential</dt>
          <dd className={config?.credential ? 'ok' : 'bad'}>
            {config?.credential ? 'configured' : 'MISSING (TABVIS_API_KEY)'}
          </dd>
          <dt>model</dt>
          <dd>{config?.model ?? '—'}</dd>
          <dt>playwright</dt>
          <dd className={config?.playwright ? 'ok' : 'bad'}>
            {config?.playwright ? 'installed' : 'not installed'}
          </dd>
          <dt>browser</dt>
          <dd>
            {config?.browser_headless ? 'headless' : 'headed'} · max {config?.max_agents} agents
          </dd>
          <dt>profile dir</dt>
          <dd>{config?.browser_profile_dir ?? '—'}</dd>
        </dl>
      </div>
    </div>
  )
}
