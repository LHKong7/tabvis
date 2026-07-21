import { Link, useNavigate } from 'react-router-dom'
import { useApp } from '../context'
import { Banner } from '../components/Banner'
import { ms } from '../format'

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="stat">
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  )
}

export function Dashboard() {
  const { health, agents } = useApp()
  const navigate = useNavigate()
  const recent = agents.slice(0, 6)
  const engine = health?.config?.browser_engine || 'chromium'

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="page-sub">Browser-native agents for the web and your codebase.</p>
        </div>
        <Link to="/run" className="btn btn-primary">
          ＋ New run
        </Link>
      </header>

      <Banner config={health?.config} onConfigure={() => navigate('/settings')} />

      <div className="stat-row">
        <Stat label="Running" value={health?.running ?? '—'} />
        <Stat label="Capacity" value={health?.capacity ?? '—'} />
        <Stat label="Sessions" value={health?.agents ?? '—'} />
        <Stat label="Open browsers" value={health?.browsers ?? '—'} />
        <Stat label="Engine" value={engine} />
      </div>

      <section className="card">
        <div className="card-head">
          <h2>Recent sessions</h2>
          <Link to="/sessions" className="link">
            View all →
          </Link>
        </div>
        {recent.length === 0 ? (
          <div className="empty">
            No runs yet. <Link to="/run" className="link">Start one →</Link>
          </div>
        ) : (
          <div className="agents">
            {recent.map((a) => (
              <button className="agent" key={a.agent_id} onClick={() => navigate(`/sessions/${a.agent_id}`)}>
                <div className="top">
                  <span className={`status s-${a.status}`}>{a.status}</span>
                  <span className="id">{a.agent_id}</span>
                </div>
                <p className="p">{a.prompt}</p>
                <div className="meta">
                  {a.turns} turns · {a.tool_calls} tools · {ms(a.duration_ms)}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
