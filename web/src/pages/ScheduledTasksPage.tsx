import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api, apiErrorMessage } from '../api'
import { useApp } from '../context'
import type { ScheduledTask, ScheduleType } from '../types'

const EMPTY_TARGET = ''

function localDateTimeValue(value: Date): string {
  const offset = value.getTimezoneOffset() * 60_000
  return new Date(value.getTime() - offset).toISOString().slice(0, 19)
}

function defaultStart(): string {
  return localDateTimeValue(new Date(Date.now() + 5 * 60_000))
}

function shownDate(value?: string | null): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

function intervalParts(seconds?: number | null): { value: string; unit: string } {
  const amount = seconds || 3600
  if (amount % 86400 === 0) return { value: String(amount / 86400), unit: 'days' }
  if (amount % 3600 === 0) return { value: String(amount / 3600), unit: 'hours' }
  return { value: String(Math.max(1, amount / 60)), unit: 'minutes' }
}

function intervalLabel(seconds?: number | null): string {
  const parts = intervalParts(seconds)
  return `Every ${parts.value} ${parts.unit}`
}

export function ScheduledTasksPage() {
  const { agents, ready } = useApp()
  const [tasks, setTasks] = useState<ScheduledTask[]>([])
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('Open example.com and summarize what changed.')
  const [scheduleType, setScheduleType] = useState<ScheduleType>('interval')
  const [startAt, setStartAt] = useState(defaultStart)
  const [intervalValue, setIntervalValue] = useState('1')
  const [intervalUnit, setIntervalUnit] = useState('hours')
  const [targetAgent, setTargetAgent] = useState(EMPTY_TARGET)
  const [profile, setProfile] = useState('')
  const [model, setModel] = useState('')
  const [maxTurns, setMaxTurns] = useState('')
  const [enabled, setEnabled] = useState(true)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const response = await api.scheduledTasks()
      setTasks(response.scheduled_tasks || [])
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load scheduled tasks')
    }
  }, [])

  useEffect(() => {
    let stopped = false
    const tick = async () => {
      if (!stopped) await load()
    }
    tick()
    const timer = window.setInterval(tick, 4000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [load])

  const intervalSeconds = useMemo(() => {
    const value = Number(intervalValue)
    const multiplier = intervalUnit === 'days' ? 86400 : intervalUnit === 'hours' ? 3600 : 60
    return Math.round(value * multiplier)
  }, [intervalValue, intervalUnit])

  const reset = () => {
    setName('')
    setPrompt('Open example.com and summarize what changed.')
    setScheduleType('interval')
    setStartAt(defaultStart())
    setIntervalValue('1')
    setIntervalUnit('hours')
    setTargetAgent(EMPTY_TARGET)
    setProfile('')
    setModel('')
    setMaxTurns('')
    setEnabled(true)
    setEditingId(null)
    setError(null)
  }

  const edit = (task: ScheduledTask) => {
    setEditingId(task.scheduled_task_id)
    setName(task.name)
    setPrompt(task.prompt)
    setScheduleType(task.schedule_type)
    const timestamp = task.next_run_at || task.run_at
    setStartAt(timestamp ? localDateTimeValue(new Date(timestamp)) : defaultStart())
    const parts = intervalParts(task.interval_seconds)
    setIntervalValue(parts.value)
    setIntervalUnit(parts.unit)
    setTargetAgent(task.resume_agent_id || EMPTY_TARGET)
    setProfile(task.profile || '')
    setModel(task.model || '')
    setMaxTurns(task.max_turns ? String(task.max_turns) : '')
    setEnabled(task.enabled)
    setError(null)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError(null)
    const date = new Date(startAt)
    if (!name.trim() || !prompt.trim() || Number.isNaN(date.getTime())) {
      setError('Name, prompt, and a valid start time are required.')
      return
    }
    if (scheduleType === 'interval' && intervalSeconds < 60) {
      setError('Recurring tasks must wait at least one minute between runs.')
      return
    }
    const body: Record<string, unknown> = {
      name: name.trim(),
      prompt: prompt.trim(),
      schedule_type: scheduleType,
      run_at: date.toISOString(),
      enabled,
      resume_agent_id: targetAgent || null,
      profile: targetAgent ? null : profile || null,
      model: model.trim() || null,
      max_turns: maxTurns ? Number(maxTurns) : null,
    }
    if (scheduleType === 'interval') body.interval_seconds = intervalSeconds
    setSaving(true)
    const response = editingId
      ? await api.updateScheduledTask(editingId, body)
      : await api.createScheduledTask(body)
    setSaving(false)
    if (!response.ok) {
      setError(apiErrorMessage(response.body, 'Could not save scheduled task'))
      return
    }
    reset()
    await load()
  }

  const act = async (
    task: ScheduledTask,
    operation: 'run' | 'toggle' | 'delete',
  ) => {
    if (
      operation === 'delete' &&
      !window.confirm(`Delete scheduled task “${task.name}”?`)
    ) {
      return
    }
    setBusyId(task.scheduled_task_id)
    setError(null)
    const response =
      operation === 'run'
        ? await api.runScheduledTask(task.scheduled_task_id)
        : operation === 'toggle'
          ? await api.updateScheduledTask(task.scheduled_task_id, {
              enabled: !task.enabled,
            })
          : await api.deleteScheduledTask(task.scheduled_task_id)
    setBusyId(null)
    if (!response.ok) {
      setError(apiErrorMessage(response.body, `Could not ${operation} scheduled task`))
      return
    }
    await load()
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Scheduled tasks</h1>
          <p className="page-sub">
            Run browser prompts once or on an interval, with a fresh or resumed session.
          </p>
        </div>
      </header>

      <form className="card schedule-form" onSubmit={submit}>
        <h2>{editingId ? 'Edit scheduled task' : 'Create scheduled task'}</h2>
        <div className="body">
          <div className="row">
            <div>
              <label>Name</label>
              <input
                value={name}
                maxLength={120}
                placeholder="Daily account check"
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div>
              <label>Execution</label>
              <select
                value={targetAgent}
                onChange={(event) => setTargetAgent(event.target.value)}
              >
                <option value="">Fresh Agent and browser session each run</option>
                {agents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>
                    Resume {agent.agent_id} · {agent.status} ·{' '}
                    {(agent.prompt || 'previous run').slice(0, 34)}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="mt12">
            <label>Prompt</label>
            <textarea
              value={prompt}
              rows={4}
              placeholder="Describe the browser task"
              onChange={(event) => setPrompt(event.target.value)}
            />
          </div>

          <div className="schedule-grid mt12">
            <div>
              <label>Schedule</label>
              <select
                value={scheduleType}
                onChange={(event) => setScheduleType(event.target.value as ScheduleType)}
              >
                <option value="once">Run once</option>
                <option value="interval">Repeat on an interval</option>
              </select>
            </div>
            <div>
              <label>{scheduleType === 'once' ? 'Run at' : 'First run'}</label>
              <input
                type="datetime-local"
                step="1"
                value={startAt}
                onChange={(event) => setStartAt(event.target.value)}
              />
            </div>
            {scheduleType === 'interval' && (
              <>
                <div>
                  <label>Repeat every</label>
                  <input
                    type="number"
                    min="1"
                    step="any"
                    value={intervalValue}
                    onChange={(event) => setIntervalValue(event.target.value)}
                  />
                </div>
                <div>
                  <label>Unit</label>
                  <select
                    value={intervalUnit}
                    onChange={(event) => setIntervalUnit(event.target.value)}
                  >
                    <option value="minutes">Minutes</option>
                    <option value="hours">Hours</option>
                    <option value="days">Days</option>
                  </select>
                </div>
              </>
            )}
          </div>

          {!targetAgent && (
            <div className="mt12">
              <label>Browser profile</label>
              <select value={profile} onChange={(event) => setProfile(event.target.value)}>
                <option value="">isolated (fresh, parallel)</option>
                <option value="default">default (logged-in, exclusive)</option>
              </select>
            </div>
          )}
          {targetAgent && (
            <p className="hint mt12">
              The task resolves this Agent&apos;s latest Run when it fires, then reuses the same
              Session, browser, profile, and transcript lineage.
            </p>
          )}

          <div className="schedule-grid mt12">
            <div>
              <label>Model</label>
              <input
                value={model}
                placeholder="default"
                onChange={(event) => setModel(event.target.value)}
              />
            </div>
            <div>
              <label>Max turns</label>
              <input
                type="number"
                min="1"
                value={maxTurns}
                placeholder="∞"
                onChange={(event) => setMaxTurns(event.target.value)}
              />
            </div>
            <label className="schedule-enabled">
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => setEnabled(event.target.checked)}
              />
              Enabled after save
            </label>
          </div>

          <div className="actions">
            <button
              className="primary"
              type="submit"
              disabled={saving || !name.trim() || !prompt.trim()}
            >
              {saving ? 'Saving…' : editingId ? 'Save changes' : 'Create task'}
            </button>
            {editingId && (
              <button type="button" onClick={reset}>
                Cancel edit
              </button>
            )}
            <span className="hint">
              One task never overlaps its previous Run. Minimum interval: 1 minute.
            </span>
          </div>
          {error && <div className="err">{error}</div>}
        </div>
      </form>

      <section className="card">
        <div className="card-head">
          <h2>Saved tasks</h2>
          <span className="hint">{tasks.length} total</span>
        </div>
        {tasks.length === 0 ? (
          <div className="empty">No scheduled tasks yet.</div>
        ) : (
          <div className="schedule-list">
            {tasks.map((task) => (
              <article className="schedule-item" key={task.scheduled_task_id}>
                <div className="schedule-main">
                  <div className="schedule-title">
                    <span className={`status ${task.enabled ? 's-running' : 's-cancelled'}`}>
                      {task.enabled ? 'enabled' : 'paused'}
                    </span>
                    <strong>{task.name}</strong>
                    <span className="schedule-id">{task.scheduled_task_id}</span>
                  </div>
                  <p className="schedule-prompt">{task.prompt}</p>
                  <div className="schedule-meta">
                    <span>
                      {task.schedule_type === 'once'
                        ? `Once · ${shownDate(task.run_at)}`
                        : intervalLabel(task.interval_seconds)}
                    </span>
                    <span>Next · {shownDate(task.next_run_at)}</span>
                    <span>
                      {task.resume_agent_id
                        ? `Resumes ${task.resume_agent_id}`
                        : `Fresh session · ${task.profile || 'isolated'}`}
                    </span>
                  </div>
                  {(task.last_run_id || task.last_error) && (
                    <div className="schedule-last">
                      <span>Last · {task.last_status || 'unknown'}</span>
                      {task.last_agent_id ? (
                        <Link to={`/sessions/${task.last_agent_id}`}>
                          {task.last_run_id}
                        </Link>
                      ) : (
                        task.last_run_id && <span>{task.last_run_id}</span>
                      )}
                      {task.last_error && (
                        <span className="errtext">{task.last_error}</span>
                      )}
                    </div>
                  )}
                </div>
                <div className="schedule-actions">
                  <button
                    type="button"
                    disabled={busyId === task.scheduled_task_id || !ready}
                    onClick={() => act(task, 'run')}
                  >
                    Run now
                  </button>
                  <button
                    type="button"
                    disabled={busyId === task.scheduled_task_id}
                    onClick={() => act(task, 'toggle')}
                  >
                    {task.enabled ? 'Pause' : 'Enable'}
                  </button>
                  <button
                    type="button"
                    disabled={busyId === task.scheduled_task_id}
                    onClick={() => edit(task)}
                  >
                    Edit
                  </button>
                  <button
                    className="danger"
                    type="button"
                    disabled={busyId === task.scheduled_task_id}
                    onClick={() => act(task, 'delete')}
                  >
                    Delete
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
