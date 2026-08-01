import { useEffect, useState } from 'react'
import { api, apiErrorMessage } from '../api'
import type { InteractionQuestion, InteractionRecord } from '../types'

interface Props {
  agentId: string
  interaction: InteractionRecord
  onAnswered: () => void
}

export function InteractionCard({ agentId, interaction, onAnswered }: Props) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  const [multi, setMulti] = useState<Record<string, string[]>>({})
  const [other, setOther] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setAnswers({})
    setMulti({})
    setOther({})
    setError('')
  }, [interaction.interaction_id])

  const respond = async (payload: Record<string, unknown>) => {
    setBusy(true)
    setError('')
    const result = await api.respondInteraction(agentId, interaction.interaction_id, payload)
    setBusy(false)
    if (!result.ok) {
      setError(apiErrorMessage(result.body, 'Could not submit the response'))
      return
    }
    onAnswered()
  }

  if (interaction.kind === 'approval') {
    return (
      <div className="card interaction-card">
        <h2>Approval required</h2>
        <div className="body">
          <p>{interaction.request.message || `${interaction.request.tool || 'A tool'} needs approval.`}</p>
          <div className="interaction-tool mono">{interaction.request.tool || 'tool'}</div>
          {interaction.request.input && (
            <pre className="interaction-input">{JSON.stringify(interaction.request.input, null, 2)}</pre>
          )}
          <div className="actions">
            <button className="primary" disabled={busy} onClick={() => respond({ allow: true })}>
              {busy ? 'Submitting…' : 'Allow once'}
            </button>
            <button className="danger" disabled={busy} onClick={() => respond({ allow: false })}>
              Deny
            </button>
          </div>
          {error && <div className="err">{error}</div>}
        </div>
      </div>
    )
  }

  const questions = interaction.request.questions || []
  const questionAnswer = (q: InteractionQuestion): string => {
    const custom = (other[q.question] || '').trim()
    if (custom) return custom
    return q.multiSelect ? (multi[q.question] || []).join(', ') : answers[q.question] || ''
  }
  const ready = questions.length > 0 && questions.every((q) => questionAnswer(q))

  return (
    <div className="card interaction-card">
      <h2>Agent needs your input</h2>
      <div className="body">
        {questions.map((q) => (
          <fieldset key={q.question} className="interaction-question">
            <legend>{q.header || 'Question'}</legend>
            <p>{q.question}</p>
            {q.options.map((option) => (
              <label key={option.label} className="interaction-option">
                <input
                  type={q.multiSelect ? 'checkbox' : 'radio'}
                  name={q.question}
                  value={option.label}
                  checked={
                    q.multiSelect
                      ? (multi[q.question] || []).includes(option.label)
                      : answers[q.question] === option.label
                  }
                  onChange={(event) => {
                    setOther((old) => ({ ...old, [q.question]: '' }))
                    if (!q.multiSelect) {
                      setAnswers((old) => ({ ...old, [q.question]: option.label }))
                      return
                    }
                    setMulti((old) => {
                      const selected = old[q.question] || []
                      return {
                        ...old,
                        [q.question]: event.target.checked
                          ? [...selected, option.label]
                          : selected.filter((item) => item !== option.label),
                      }
                    })
                  }}
                />
                <span>
                  {option.label}
                  {option.description && <small>{option.description}</small>}
                </span>
              </label>
            ))}
            <input
              value={other[q.question] || ''}
              placeholder="Other answer (optional)"
              onChange={(event) =>
                setOther((old) => ({ ...old, [q.question]: event.target.value }))
              }
            />
          </fieldset>
        ))}
        <div className="actions">
          <button
            className="primary"
            disabled={busy || !ready}
            onClick={() =>
              respond(
                Object.fromEntries(questions.map((q) => [q.question, questionAnswer(q)])),
              )
            }
          >
            {busy ? 'Submitting…' : 'Send answer'}
          </button>
        </div>
        {error && <div className="err">{error}</div>}
      </div>
    </div>
  )
}
