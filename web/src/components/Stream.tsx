import { useEffect, useRef } from 'react'
import type { Frame } from '../types'

interface StreamProps {
  frames: Frame[]
  title?: string
  emptyMessage?: string
}

export function Stream({
  frames,
  title = 'Live stream',
  emptyMessage = 'Events appear here while an agent runs.',
}: StreamProps) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight
  }, [frames.length])
  return (
    <div className="card">
      <h2>{title}</h2>
      <div className="stream" ref={box}>
        {frames.length === 0 ? (
          <div className="muted">{emptyMessage}</div>
        ) : (
          frames.map((f, i) => (
            <div className={`ev e-${f.cls || f.event}`} key={i}>
              <span className="k">{f.event}</span>
              <span className="v">{f.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
