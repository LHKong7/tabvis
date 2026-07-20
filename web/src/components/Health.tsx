import type { Health as HealthT } from '../types'

export function Health({ h }: { h: HealthT | null }) {
  if (!h) return <span className="pill">connecting…</span>
  return (
    <span className="pill">
      running <b>{h.running}</b> / {h.max_agents} · capacity <b>{h.capacity}</b> · total <b>{h.agents}</b>
    </span>
  )
}
