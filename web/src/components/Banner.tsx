import type { HealthConfig } from '../types'

// Tells you the server can't reach a model BEFORE you hit Run, instead of every run dying with an
// opaque API error. `missing` comes from GET /health -> config.
export function Banner({ config, onConfigure }: { config?: HealthConfig; onConfigure: () => void }) {
  if (!config || config.ready) return null
  return (
    <div className="banner full">
      <span className="ico">⚠</span>
      <div>
        <b>This server can't reach a model yet.</b>
        <p>
          Missing{' '}
          {config.missing?.map((m, i) => (
            <span key={m}>
              {i ? ', ' : ''}
              <code>{m}</code>
            </span>
          ))}{' '}
          — every run will fail until they're set. Set them here and they apply immediately, no restart.
        </p>
        <div className="actions">
          <button className="primary" onClick={onConfigure}>
            Configure now
          </button>
        </div>
      </div>
    </div>
  )
}
