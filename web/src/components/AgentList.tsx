import { useState } from 'react'
import type { AgentSummary } from '../types'
import { ms } from '../format'

interface Props {
  agents: AgentSummary[]
  selected: string | null
  onSelect: (id: string) => void
}

const FILTERS = ['all', 'running', 'completed', 'failed', 'cancelled', 'queued']

export function AgentList({ agents, selected, onSelect }: Props) {
  const [filter, setFilter] = useState('all')
  const shown = filter === 'all' ? agents : agents.filter((a) => a.status === filter)

  return (
    <div className="card">
      <h2>Sessions ({shown.length})</h2>
      <div className="filters">
        {FILTERS.map((f) => (
          <button
            key={f}
            type="button"
            className={f === filter ? 'chip active' : 'chip'}
            onClick={() => setFilter(f)}
          >
            {f}
            {f !== 'all' ? ` ${agents.filter((a) => a.status === f).length}` : ''}
          </button>
        ))}
      </div>
      <div className="agents">
        {shown.length === 0 ? (
          <div className="empty">{filter === 'all' ? 'No runs yet.' : `No ${filter} runs.`}</div>
        ) : (
          shown.map((a) => (
            <button
              className="agent"
              key={a.agent_id}
              aria-selected={a.agent_id === selected}
              onClick={() => onSelect(a.agent_id)}
            >
              <div className="top">
                <span className={`status s-${a.status}`}>{a.status}</span>
                <span className="id">{a.agent_id}</span>
              </div>
              <p className="p">{a.prompt}</p>
              <div className="meta">
                {a.turns} turns · {a.tool_calls} tools · {ms(a.duration_ms)}
                {a.profile ? ` · ${a.profile}` : ' · isolated'}
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
