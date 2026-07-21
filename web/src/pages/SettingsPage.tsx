import { useApp } from '../context'
import { Settings } from '../components/Settings'

export function SettingsPage() {
  const { health, refreshHealth } = useApp()
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="page-sub">Model endpoint, browser, workspace and more — applied live, no restart.</p>
        </div>
      </header>
      <Settings open config={health?.config} onSaved={refreshHealth} />
    </div>
  )
}
