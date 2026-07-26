import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApp } from '../context'
import { api } from '../api'
import { Detail } from '../components/Detail'
import { InteractionCard } from '../components/InteractionCard'
import { Stream } from '../components/Stream'
import type { AgentRecord, BrowserView, InteractionRecord } from '../types'

export function SessionDetailPage() {
  const { id } = useParams<{ id: string }>()
  const { frames, streamFor, cancel, quit, cancelling, setRunOn } = useApp()
  const navigate = useNavigate()
  const [agent, setAgent] = useState<AgentRecord | null>(null)
  const [browser, setBrowser] = useState<BrowserView | null>(null)
  const [interactions, setInteractions] = useState<InteractionRecord[]>([])

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

  const onContinue = (aid: string) => {
    setRunOn(aid)
    navigate('/run')
  }

  const isLive = id === streamFor

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
          <Stream frames={isLive ? frames : []} />
          {!isLive && (
            <p className="hint" style={{ marginTop: '8px' }}>
              Live output shows here only while this session is the one running. Its record and browser
              trail are on the right; use <b>Continue</b> to send it a new prompt.
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
