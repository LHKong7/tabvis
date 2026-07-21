import { useApp } from '../context'
import { Driver } from '../components/Driver'

export function DriversPage() {
  const { health, refreshHealth } = useApp()
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Browser</h1>
          <p className="page-sub">Pick the engine, download drivers, and manage live browsers.</p>
        </div>
      </header>
      <Driver open config={health?.config} onChanged={refreshHealth} />
    </div>
  )
}
