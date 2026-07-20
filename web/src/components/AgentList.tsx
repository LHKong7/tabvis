import type { AgentSummary } from '../types'
import { ms } from '../format'

interface Props {
  agents: AgentSummary[]
  selected: string | null
  onSelect: (id: string) => void
}

export function AgentList({ agents, selected, onSelect }: Props) {
  return (
    <div className="card">
      <h2>Agents ({agents.length})</h2>
      <div className="agents">
        {agents.length === 0 ? (
          <div className="empty">No runs yet.</div>
        ) : (
          agents.map((a) => (
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
