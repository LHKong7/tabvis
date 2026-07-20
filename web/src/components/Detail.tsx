import type { AgentRecord, BrowserInfo, BrowserView } from '../types'
import { ms, clock } from '../format'

interface Props {
  agent: AgentRecord | null
  browser: BrowserView | null
  onCancel: (id: string) => void
  onQuit: (id: string) => void
  onContinue: (id: string) => void
  cancelling: boolean
}

export function Detail({ agent, browser, onCancel, onQuit, onContinue, cancelling }: Props) {
  if (!agent)
    return (
      <div className="card">
        <h2>Agent</h2>
        <div className="empty">Select an agent, or start a new run.</div>
      </div>
    )

  const b: BrowserInfo = browser?.browser || {}
  const live = !['completed', 'failed', 'cancelled'].includes(agent.status)
  // The bundled browser outlives the run: even a finished agent may still hold one open, so Quit
  // (end the agent + close its browser, freeing the profile) stays enabled after Cancel does not.
  const hasBrowser = !!(browser && browser.browser && Object.keys(browser.browser).length)
  return (
    <>
      <div className="card">
        <h2>Agent · {agent.agent_id}</h2>
        <div className="body">
          <div className="detail-head">
            <span className={`status s-${agent.status}`}>{agent.status}</span>
            <span className="spacer"></span>
            <button
              title="Send a new prompt to this agent — continues its session, browser & profile"
              onClick={() => onContinue(agent.agent_id)}
            >
              Continue
            </button>
            <button disabled={!live || cancelling} onClick={() => onCancel(agent.agent_id)}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
            <button
              className="danger"
              disabled={cancelling || (!live && !hasBrowser)}
              title="End this agent and close its bundled browser"
              onClick={() => onQuit(agent.agent_id)}
            >
              Quit
            </button>
          </div>
          <dl>
            <dt>agent_id</dt>
            <dd>{agent.agent_id}</dd>
            <dt>session_id</dt>
            <dd>{agent.session_id || '—'}</dd>
            <dt>model</dt>
            <dd>{agent.model || 'default'}</dd>
            <dt>profile</dt>
            <dd>{agent.profile || 'isolated'}</dd>
            <dt>turns / tools</dt>
            <dd>
              {agent.turns} / {agent.tool_calls}
            </dd>
            <dt>started</dt>
            <dd>
              {clock(agent.started_at)} · {ms(agent.duration_ms)}
            </dd>
            {agent.result && (
              <>
                <dt>result</dt>
                <dd>{agent.result}</dd>
              </>
            )}
            {agent.error && (
              <>
                <dt>error</dt>
                <dd className="errtext">{agent.error}</dd>
              </>
            )}
          </dl>
        </div>
      </div>

      <div className="card">
        <h2>Browser {b.status ? `· ${b.status}` : ''}</h2>
        <div className="body">
          {!browser || !browser.browser ? (
            <div className="muted">No browser for this agent yet.</div>
          ) : (
            <>
              <dl>
                <dt>engine</dt>
                <dd>
                  {b.engine || '—'} {b.version || ''}
                </dd>
                <dt>headless</dt>
                <dd>{String(b.headless)}</dd>
                <dt>profile dir</dt>
                <dd>{b.profile_dir || '—'}</dd>
                <dt>tabs</dt>
                <dd>{(browser.tabs || []).length}</dd>
              </dl>
              {(browser.history || []).length > 0 && (
                <div className="mt12">
                  <label>Visited ({browser.history!.length})</label>
                  <div className="hist">
                    {browser
                      .history!.slice()
                      .reverse()
                      .map((v, i) => (
                        <a key={i} href={v.url} target="_blank" rel="noreferrer" title={v.url}>
                          {v.title || v.url}
                        </a>
                      ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  )
}
