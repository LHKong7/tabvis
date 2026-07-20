// Presentation helpers shared by the panels.

export const ms = (n?: number | null): string =>
  n == null ? '—' : n < 1000 ? `${n}ms` : `${(n / 1000).toFixed(1)}s`

export const clock = (t?: number | string): string => (t ? new Date(t).toLocaleTimeString() : '—')

export type Summary = string | { text: string; cls: string } | null

// Turn a raw SSE frame into a display line. Returns null for noise we hide (empty assistant turns,
// duplicate `user` frames, deltas). Returns an object with an explicit class for the final `result`,
// which colours itself by is_error.
export function summarize(ev: string, d: any): Summary {
  switch (ev) {
    case '_id':
      return `agent ${d.agent_id}`
    case 'agent':
      return `${d.agent_id} · ${d.model ?? 'default model'}`
    case 'system':
      return `session ${String(d.session_id ?? '').slice(0, 8)}… · ${(d.tools || []).length} tools`
    case 'assistant':
      return (
        (d.message?.content || [])
          .filter((b: any) => b.type === 'text' && b.text?.trim())
          .map((b: any) => b.text)
          .join('\n') || null
      ) // null => hide empty turns
    case 'tool_use':
      return `${d.name}(${JSON.stringify(d.input ?? {}).slice(0, 110)})`
    case 'tool_result':
      return (d.is_error ? '✗ ' : '') + String(d.content ?? '').split('\n').slice(0, 4).join('  ')
    case 'result':
      return { text: `${d.is_error ? '✗ ' : '✓ '}${d.result ?? ''}`, cls: d.is_error ? 'error' : 'result' }
    case 'error':
      return d.message
    case 'cancelled':
      return 'cancelled'
    case 'done':
      return `stream closed (${d.status ?? 'done'})`
    case 'user':
      return null // duplicate of tool_result — hide
    case 'delta':
      return null
    default:
      return JSON.stringify(d).slice(0, 140)
  }
}
