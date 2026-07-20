import { useEffect, useRef } from 'react'
import type { Frame } from '../types'

export function Stream({ frames }: { frames: Frame[] }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight
  }, [frames.length])
  return (
    <div className="card">
      <h2>Live stream</h2>
      <div className="stream" ref={box}>
        {frames.length === 0 ? (
          <div className="muted">Events appear here while an agent runs.</div>
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
