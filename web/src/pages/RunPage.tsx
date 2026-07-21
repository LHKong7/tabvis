import { useApp } from '../context'
import { NewRun } from '../components/NewRun'

export function RunPage() {
  const { launch, busy, ready, agents, health, runOn, setRunOn, refreshHealth } = useApp()
  return (
    <div className="page page-narrow">
      <header className="page-head">
        <div>
          <h1>New run</h1>
          <p className="page-sub">Launch a fresh agent, or continue an existing session.</p>
        </div>
      </header>
      <NewRun
        onLaunched={launch}
        busy={busy}
        ready={ready}
        agents={agents}
        config={health?.config}
        runOn={runOn}
        onRunOn={setRunOn}
        onEngineChanged={refreshHealth}
      />
    </div>
  )
}
