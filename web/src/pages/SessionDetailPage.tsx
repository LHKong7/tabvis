import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApp } from '../context'
import { api } from '../api'
import { Detail } from '../components/Detail'
import { InteractionCard } from '../components/InteractionCard'
import { Stream } from '../components/Stream'
import { frameFor } from '../format'
import type { AgentRecord, BrowserView, Frame, InteractionRecord } from '../types'

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { framesFor, cancel, quit, cancelling, setRunOn } = useApp()
  const navigate = useNavigate()
  const [agent, setAgent] = useState<AgentRecord | null>(null)
  const [browser, setBrowser] = useState<BrowserView | null>(null)
  const [interactions, setInteractions] = useState<InteractionRecord[]>([])
  const [historyFrames, setHistoryFrames] = useState<Frame[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)

  // Poll this session's record + browser while the page is open.
  useEffect(() => {
    if (!id) return
    let stop = false
    const tick = async () => {
      const [a, b, pending] = await Promise.all([
        api.get(id),
        api.browser(id),
        api.interactions(id),
      ])
      if (!stop) {
        setAgent(a)
        setBrowser(b)
        setInteractions(pending.interactions || [])
      }
    }
    tick()
    const t = setInterval(tick, 1500)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [id])

  // Replay the latest Run's durable event log. The in-memory live stream is still preferred for
  // the run launched in this browser session, while this replay covers refreshes, older sessions,
  // and switching between several concurrently-running agents.
  useEffect(() => {
    if (!id) return
    let stop = false
    const tick = () => {
      api.events(id)
        .then((events) => {
          if (stop) return
          setHistoryFrames(
            events
              .map(({ event, data }) => frameFor(event, data))
              .filter((frame): frame is Frame => frame !== null),
          )
        })
        .catch(() => {
          if (!stop) setHistoryFrames([])
        })
        .finally(() => {
          if (!stop) setHistoryLoading(false)
        })
    }
    setHistoryFrames([])
    setHistoryLoading(true)
    tick()
    const timer = setInterval(tick, 1500)
    return () => {
      stop = true
      clearInterval(timer)
    }
  }, [id])

  const onContinue = (aid: string) => {
    setRunOn(aid)
    navigate('/run')
  }

  const liveFrames = framesFor(id)
  const shownFrames = liveFrames.length > 0 ? liveFrames : historyFrames
  const isRunningLive = agent?.status === 'running' && liveFrames.length > 0

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Session</h1>
          <p className="page-sub mono">{id}</p>
        </div>
        <Link to="/sessions" className="link">
          ← All sessions
        </Link>
      </header>
      <div className="split">
        <div className="split-main">
          {interactions.map((interaction) => (
            <InteractionCard
              key={interaction.interaction_id}
              agentId={id || ''}
              interaction={interaction}
              onAnswered={() =>
                setInteractions((current) =>
                  current.filter((item) => item.interaction_id !== interaction.interaction_id),
                )
              }
            />
          ))}
          <Stream
            frames={shownFrames}
            title={isRunningLive ? 'Live stream' : 'Run history'}
            emptyMessage={historyLoading ? 'Loading durable run history…' : 'No persisted run events.'}
          />
          {!isRunningLive && historyFrames.length > 0 && (
            <p className="hint" style={{ marginTop: '8px' }}>
              Replayed from the durable run event log. Use <b>Continue</b> to send this agent a new
              prompt.
            </p>
          )}
        </div>
        <div className="split-side">
          <Detail
            agent={agent}
            browser={browser}
            onCancel={cancel}
            onQuit={quit}
            onContinue={onContinue}
            cancelling={cancelling}
          />
        </div>
      </div>
    </div>
  )
}
