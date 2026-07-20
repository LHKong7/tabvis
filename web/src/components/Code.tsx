import { useState } from 'react'

export function Code({ children }: { children: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    // navigator.clipboard needs a secure context AND a permission grant — it rejects silently on
    // plain http (e.g. --host 0.0.0.0) and in some embedded views. Fall back to execCommand.
    const done = () => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    }
    try {
      await navigator.clipboard.writeText(children)
      return done()
    } catch {
      /* fall through */
    }
    const ta = document.createElement('textarea')
    ta.value = children
    ta.setAttribute('readonly', '')
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
      done()
    } catch {
      /* give up quietly */
    }
    document.body.removeChild(ta)
  }
  return (
    <div className="code">
      {children}
      <button onClick={copy}>{copied ? 'copied' : 'copy'}</button>
    </div>
  )
}
