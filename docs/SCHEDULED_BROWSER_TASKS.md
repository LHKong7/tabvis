# Scheduled Browser Tasks

Tabvis can persist a browser-agent prompt and execute it later from the Web console. A task can run
once at a specific time or repeatedly on a fixed interval. Each firing creates a normal, immutable
Gateway Run, so scheduled executions appear in **Sessions** and use the same browser tools, policy
checks, event log, cancellation flow, and result storage as a manually started Run.

## Start the console

```bash
uv run tabvis
```

Open `http://127.0.0.1:8765/` and select **Scheduled tasks** in the sidebar. The page is available in
the normal Web server even if the optional `TABVIS_GATEWAY` command/event surface is disabled.

## Create a task

The form accepts:

- **Name** — a short label shown in the task list.
- **Prompt** — the complete instruction sent to the browser agent.
- **Execution** — either a fresh Agent and browser session for every firing, or an existing Agent
  whose latest Session should be resumed.
- **Schedule** — `Run once` or `Repeat on an interval`.
- **First run / Run at** — entered in the browser's local timezone and stored as UTC.
- **Interval** — minutes, hours, or days. The minimum interval is one minute.
- **Browser profile** — used only for fresh executions. `isolated` creates an independent profile;
  `default` uses the persistent logged-in profile and remains subject to the normal exclusive-profile
  lease.
- **Model** and **Max turns** — optional per-task overrides.
- **Enabled after save** — a task may be created or saved in a paused state.

Saved tasks can be run immediately, paused, enabled, edited, or deleted. **Run now** does not change
the next automatic occurrence.

## Fresh and resumed execution

### Fresh execution

When no existing Agent is selected, every occurrence receives a new `agent_id` and `session_id`.
This is appropriate for independent research, monitoring, and public-page checks that should not
share cookies or conversation history.

### Resume an old Session

When an Agent is selected, Tabvis resolves that Agent's latest Run at the moment the schedule fires.
The new Run keeps:

- the same durable Agent ID;
- the latest Run's Session ID;
- the Agent's browser profile and browser workspace;
- the existing transcript lineage, using `resume_mode=plus`.

The scheduled execution is still a new immutable Run. It does not overwrite the earlier Run.
Consequently, the complete history remains visible while the agent can continue an authenticated or
multi-step browser workflow.

If the selected Agent has been deleted, or if its previous scheduled Run is still active, the
occurrence is not started and the error is shown in the task list.

## Scheduling and recovery behavior

Scheduled tasks are stored in the authoritative `gateway.db` database in the
`scheduled_tasks` table. They survive server restarts.

The scheduler starts with the Web server and polls due records once per second. A database-backed
claim prevents duplicate dispatch inside the process. At startup, incomplete claims from a stopped
process are released. Every automatic occurrence also uses a deterministic command ID derived from
the task ID and scheduled timestamp. If the process stopped after creating the Run but before
updating the task record, replay returns the original Run instead of creating a second one.

One-time tasks disable themselves after a dispatched automatic occurrence. If dispatch is
temporarily blocked by capacity or an overlapping Run, they remain enabled and retry after one
minute. Interval tasks calculate the next future occurrence from the previous scheduled timestamp,
skipping stale intermediate timestamps after a long shutdown.

## Safety and load control

Scheduling does not bypass Tabvis browser policy or rate limiting. In addition:

- intervals shorter than 60 seconds are rejected;
- the scheduler respects the Gateway's maximum concurrent Run capacity;
- one scheduled task never overlaps its own previous Run;
- persistent browser profiles retain the normal one-active-writer lease;
- the existing process-wide browser request pacing remains active.

These controls prevent a recurring research prompt from becoming an accidental high-rate crawler.
Prompts should still ask the agent to browse conservatively and respect site terms.

## HTTP API

The Web console uses the following administrator-only endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/v1/scheduled-tasks` | List all tasks |
| `POST` | `/v1/scheduled-tasks` | Create a task |
| `GET` | `/v1/scheduled-tasks/{id}` | Read one task |
| `PATCH` | `/v1/scheduled-tasks/{id}` | Edit, pause, or enable a task |
| `DELETE` | `/v1/scheduled-tasks/{id}` | Delete a task |
| `POST` | `/v1/scheduled-tasks/{id}/run` | Run immediately without moving the schedule |

Example recurring task:

```json
{
  "name": "Hourly release check",
  "prompt": "Open the project's release page and summarize new stable releases.",
  "schedule_type": "interval",
  "run_at": "2026-07-27T12:00:00Z",
  "interval_seconds": 3600,
  "enabled": true,
  "profile": null,
  "model": null,
  "max_turns": 40
}
```

Example that resumes an existing Session:

```json
{
  "name": "Continue account review",
  "prompt": "Open the account dashboard and report any new alerts.",
  "schedule_type": "once",
  "run_at": "2026-07-28T01:30:00Z",
  "enabled": true,
  "resume_agent_id": "ag_0123456789ab"
}
```

All timestamps returned by the API are ISO 8601 UTC timestamps. A recurring task currently uses a
fixed elapsed-time interval; cron expressions and calendar-specific rules are not part of this
version.
