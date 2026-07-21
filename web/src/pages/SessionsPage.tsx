import { Link, useNavigate } from 'react-router-dom'
import { useApp } from '../context'
import { AgentList } from '../components/AgentList'

export function SessionsPage() {
  const { agents } = useApp()
  const navigate = useNavigate()
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Sessions</h1>
          <p className="page-sub">Every agent run — select one to inspect or continue it.</p>
        </div>
        <Link to="/run" className="btn btn-primary">
          ＋ New run
        </Link>
      </header>
      <AgentList agents={agents} selected={null} onSelect={(id) => navigate(`/sessions/${id}`)} />
    </div>
  )
}
