import { useApp } from '../context'
import { Setup } from '../components/Setup'

export function SetupPage() {
  const { health } = useApp()
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Setup</h1>
          <p className="page-sub">Install, configure a model endpoint, and run tabvis as a server.</p>
        </div>
      </header>
      <Setup open config={health?.config} />
    </div>
  )
}
