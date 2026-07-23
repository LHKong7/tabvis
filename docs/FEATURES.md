# Tabvis feature overview

What Tabvis can do: its tool surface and how each subsystem behaves. For install, configuration, and
the server/CLI reference see [RUNNING.md](RUNNING.md); for the control-plane design see
[AGENT_GATEWAY_DESIGN.md](AGENT_GATEWAY_DESIGN.md).

---

## The reasoning loop

Tabvis runs one agent loop: the model reads a goal, calls tools, observes results, and repeats until
the task is done. Each turn:

1. **Auto-compaction** runs first (fail-open) — if the context is near its limit it is summarized
   before the model call, so a run never hard-stops on context (see [Context management](#context-management)).
2. **Model call** — the provider streams an assistant message; its tool-use blocks are extracted.
3. **Completion gate** — no tool calls → the run completes.
4. **Tool execution** — each tool call is validated, permission-checked, and executed; results become
   the next user message.
5. The turn count advances (only on tool-using turns); if `--max-turns` is reached the run ends with
   an `error_max_turns` result.

The loop is headless and single-pass per invocation (no multi-turn session state across CLI calls).
`--max-turns` is unbounded by default. Transient API errors are retried with backoff
(`TABVIS_MAX_RETRIES`, default 10), and a stalled stream is caught by an idle watchdog
(`TABVIS_STREAM_IDLE_TIMEOUT`, default 90s) so silent proxy stalls retry cleanly.

---

## Built-in tools

Tabvis registers **20 built-in tool singletons** (19 enabled by default; `BrowserIntent` is
flag-gated off). Connected MCP tools join the same pool at runtime. The names below are exactly what
the model calls.

### Files

| Tool | Purpose |
|---|---|
| `Read` | Read a file — text, images (shown visually), PDFs (`pages` range), and `.ipynb` notebooks with outputs. |
| `Edit` | Exact string replacement in a file (`replace_all` optional). |
| `Write` | Create or overwrite a file. |
| `Glob` | Find files by glob pattern. |
| `Grep` | Search file contents with regex via ripgrep (`content`/`files_with_matches`/`count` modes, context flags). |
| `NotebookEdit` | Replace/insert/delete cells in a Jupyter `.ipynb`. |

### Runtime & orchestration

| Tool | Purpose |
|---|---|
| `Bash` | Run a shell command (foreground or `run_in_background`); returns merged stdout/stderr. |
| `Agent` | Delegate a scoped task to a sub-agent and get its report back (alias: `Task`). |
| `Workflow` | Run a model-authored Python orchestration script that fans out many sub-agents in the background; returns only the script's final value. |
| `TodoWrite` | Maintain the session task checklist. |
| `AskUserQuestion` | Ask the user 1–4 multiple-choice questions. |
| `ToolSearch` | Fetch full schemas for deferred / MCP tools on demand. |

### Browser

All browser tools are always-loaded (enabled whenever Playwright is available) and return a fresh
accessibility snapshot after every action. There is **no `WebFetch`** — the browser is Tabvis's web
interface, and every page is rendered with JavaScript in a real browser context.

| Tool | Purpose |
|---|---|
| `BrowserNavigate` | Open a URL or go back/forward/reload. |
| `BrowserSnapshot` | Re-capture the current page (optional screenshot). |
| `BrowserClick` | Click an element by its snapshot ref. |
| `BrowserType` | Type into an element by ref (`submit` presses Enter). |
| `BrowserWait` | Wait for text to appear/disappear, a load state, or a fixed time. |
| `BrowserDownload` | Fetch a URL through the browser session (cookies/auth apply) into the workspace to be `Read`. |
| `BrowserIntent` | *(flag-gated: `TABVIS_BROWSER_INTENTS=1`)* Drive the browser by high-level intent (`navigate`/`search`/`research`/`compare`). |

### Extensions

| Tool | Purpose |
|---|---|
| `Skill` | Invoke a project/user skill (a slash-command prompt) by name; expands into instructions the loop follows. |
| `mcp__<server>__<tool>` | One wrapped tool per connected MCP server tool (added dynamically; schema loaded on demand via `ToolSearch`). |

**Gating.** `TABVIS_SIMPLE`/`--bare` reduces the registry to `Bash`/`Read`/`Edit` and skips MCP.
`NotebookEdit`, `TodoWrite`, `AskUserQuestion`, and every MCP tool are *deferred* — their schemas are
withheld from the initial prompt and loaded on demand via `ToolSearch` to keep the prompt small.
Permission deny-rules can remove any tool.

---

## Browser subsystem

Tabvis drives a real Playwright browser and perceives pages the way an assistive technology would.

**Accessibility snapshots + stable refs.** Every browser tool returns a compact snapshot in which
each interactive/named element is tagged `[ref=eN]`. The agent acts on those refs, not on brittle CSS
selectors. Tabvis prefers Playwright's public ARIA snapshot; where that's unavailable it falls back
to a DOM-attribute tagging pass. Refs are valid only for the most recent snapshot — acting on a stale
ref returns a recoverable "snapshot again" error, and every action returns a fresh snapshot, so the
model stays in a perceive → act loop.

**Auto-visual.** When a page's accessibility tree is too sparse to act on, Tabvis automatically
attaches a screenshot plus trimmed HTML (`TABVIS_BROWSER_AUTO_VISUAL`, default on), and box
coordinates when a screenshot is present so refs line up with the image.

**Persistent sessions.** Launch-based engines keep a persistent profile (cookies, logins, tabs) that
survives across tool calls and across `tabvis` invocations. Each agent owns one browser workspace,
keyed by a profile; a profile is 1:1 with an agent so parallel agents get isolated state (Chromium's
single-writer profile lock is respected). Owned browsers are never idle-reaped; only unowned
workspaces expire (`TABVIS_BROWSER_IDLE_TIMEOUT_MS`).

**Downloads.** Files land in a per-run workspace, with names sanitized to a basename and de-collided
so a hostile `suggested_filename` can't escape the directory. Three capture paths: Playwright
download events, PDF navigations (Chromium's PDF viewer is unreadable to the accessibility tree), and
the explicit `BrowserDownload` tool. New files are announced to the agent to `Read`.

**Artifacts.** An append-only browsing trail (navigations, page snapshots, interactions, and
content-addressed DOM blobs) is recorded per session and exposed at `GET /agents/{id}/artifacts`.

**Engines & pacing.** Chromium, Firefox, WebKit, installed browsers, stealth engines (CloakBrowser,
Camoufox), and remote/CDP sessions all drive through the same tool interface. Request pacing is on by
default and shared across concurrent agents. See [RUNNING.md §6](RUNNING.md#6-browser-configuration).

---

## Model providers

One agent loop, three providers, selected by `TABVIS_MODEL_PROVIDER` or inferred from the model id:

- **Anthropic** (default) — consumes the Messages streaming format natively.
- **OpenAI / OpenAI-compatible** — Chat Completions; reaches any compatible gateway (vLLM, Groq,
  Together, local servers).
- **Gemini** — `google-genai`.

Non-Anthropic providers translate their native streaming into Anthropic-style streaming parts
internally, so tools, images, and the loop behave identically. Text-only models get an OCR fallback
for image blocks. Provider SDKs are optional extras that raise a clear install hint rather than
downgrading silently. See [RUNNING.md §5](RUNNING.md#5-model--provider-configuration).

---

## MCP (Model Context Protocol)

Connect external tool servers and use them alongside the built-in tools.

- **Config sources** merge low → high: user (`~/.tabvis` settings + `~/.tabvis.json`) → project
  (`.mcp.json`, nearest wins) → dynamic (`TABVIS_MCP_CONFIG`, a JSON document or path). `${VAR}` /
  `${VAR:-default}` expansion is applied to string values.
- **Transports:** `stdio` (default) and streamable `http` are functional; other config shapes are
  accepted but not connected in this build.
- **Merge:** each server's tools are wrapped as `mcp__<server>__<tool>`, deduped against built-ins
  (built-ins win on a name clash), and marked deferred so schemas load on demand via `ToolSearch`.
- Per-server failures are isolated — one bad server never aborts the rest. Skipped entirely under
  `TABVIS_SIMPLE`.

---

## Skills

Skills are reusable slash-command prompts that expand into instructions for the loop.

- **Discovery:** `<cwd>/.tabvis/skills/` (project, higher precedence) and `~/.tabvis/skills/` (user).
  A skill is a directory with `SKILL.md`, or a single `<name>.md`, with YAML frontmatter (`name`,
  `description`, `allowed-tools`, `model`, `argument-hint`, `when_to_use`, `user-invocable`).
- **Invocation:** the model calls the `Skill` tool (or the user types `/<name>`); the body is expanded
  with argument substitution (`$1`, `$ARGUMENTS`, named placeholders) and returned as the prompt.

---

## Sub-agents

The `Agent` tool delegates a scoped task to a fresh sub-agent that reuses the same loop, then returns
its final report. Sub-agents get their own agent id, an empty message history, and a chosen tool
subset; they share the parent's cancellation (cancelling the parent cascades) and are bounded by a
recursion-depth guard.

Built-in agent types available by default: `general-purpose` (all tools) and `statusline-setup`
(read/edit only); a `tabvis-guide` agent is added on non-SDK entrypoints. Custom agents can be
defined as Markdown files in `<cwd>/.tabvis/agents/` or `~/.tabvis/agents/` (frontmatter `name`,
`description`, `tools`, `model`); project overrides user overrides built-in.

> This in-loop `Agent` sub-agent is distinct from the server-side **agent registry** (the `agent_id`
> a `POST /agent` run gets on the HTTP API).

---

## Workflows

The `Workflow` tool runs a model-authored **Python** orchestration script that spawns many sub-agents
in the background and returns only the script's final `return` value (intermediate outputs stay in
the workflow's own transcript). It's the tool for decompose-and-fan-out work: parallel readers,
adversarial verification, loop-until-done discovery.

- **Sandboxed execution:** scripts are validated (no imports, no escape builtins) and run in a
  restricted namespace exposing an orchestration API (`agent`, `parallel`, `pipeline`, `phase`,
  `log`, `args`, `meta`).
- **Limits:** ≤16 concurrent sub-agents, ≤1000 total per workflow.
- **Pause/resume:** each completed `agent()` call is journaled; a resumed run replays cached results
  for unchanged calls instead of re-spawning.
- **Surface:** also reachable as `/dynamic-workflow <task>` (generate → save → run), and saved
  workflows become their own `/<name>` slash commands.

---

## Memory

Tabvis keeps a persistent, file-based memory across conversations (on by default; disable with
`TABVIS_DISABLE_AUTO_MEMORY`). An index file `MEMORY.md` points at individual `<name>.md` memory
files, each carrying frontmatter (`name`, `description`, `type` ∈ `user`/`feedback`/`project`/
`reference`). Memory lives under the project's memory directory
(`<config-home>/projects/<git-root>/memory/`, overridable with `TABVIS_MEMORY_PATH_OVERRIDE`).

---

## Context management

Long runs are kept inside the model's context window automatically.

- **Auto-compaction** runs before every model call. As the conversation approaches the effective
  context window (window minus a reserved-summary buffer), older messages are summarized and the run
  continues from the summary plus re-read recent files and attachments. It's wrapped so compaction
  can never crash a run, with a 3-strike circuit breaker.
- **Context window:** 200,000 tokens by default; a `[1m]` model suffix requests 1,000,000 (disable
  with `TABVIS_DISABLE_1M_CONTEXT`). Trigger thresholds are tunable via
  `TABVIS_AUTOCOMPACT_PCT_OVERRIDE` / `TABVIS_AUTO_COMPACT_WINDOW` / `TABVIS_BLOCKING_LIMIT_OVERRIDE`.

---

## Permissions & policy

Every file-write, shell, and browser action is classified by its **side-effect category** (not by
tool name) and checked against a policy engine before it runs.

**Model.** Actions like `filesystem.write`, `shell.execute`, `browser.navigate`,
`browser.download`, `network.request`, `credential.use` are matched against rules that yield
`allow` / `deny` / `ask`. Rules are merged from, low → high priority: a **mode baseline** →
`settings.json` `permissions.rules` → per-browser-identity rules → session grants. A `deny` always
wins and is never grantable; otherwise the highest-priority match wins; no match falls back to the
mode's default.

**Modes** (`TABVIS_PERMISSION_MODE` or `settings.permissions.mode`):

| Mode | Default effect | Baseline |
|---|---|---|
| `trusted` | allow | none |
| `standard` (default) | ask | allow reads/writes in the workspace; protect config; **ask** on downloads, uploads, network, credential use, clipboard, exports |
| `locked` | deny | read-only allow-list only |

**Enforcement details.** Writes are re-checked at the actual write point (closing TOCTOU gaps).
Browser navigation is additionally gated by the domain allowlist
(`TABVIS_BROWSER_ALLOWED_DOMAINS`) — in headless one-shot mode an `ask` resolves to *deny*.
Filesystem writes to config/secret paths are hard-denied even under `trusted`. Every decision is
audited (`policy.decision`; disable with `TABVIS_PERMISSION_AUDIT`), and **shadow mode**
(`TABVIS_PERMISSION_SHADOW`) records what *would* have happened without ever blocking.

**Scope notes.** File *reads* are not gated at the tool boundary — read confinement comes from
working-directory restrictions, not the policy engine. The OS-level sandbox is not bundled in this
build, so `sandbox.*` settings build a config but do not enforce process/network isolation; the real
enforcement is workspace confinement (the agent operates within its working directory + registered
download workspace, with symlink-aware path resolution).

See [RUNNING.md §8.2](RUNNING.md#82-permissions--policy) for the env-var knobs.

---

## Agent Gateway

The **Agent Gateway** (`tabvis/gateway/`) is a control plane in front of the runtime, mounted next to
the legacy HTTP server by default (`TABVIS_GATEWAY=1`). It exists to make agent execution durable,
observable, and multi-channel.

**Core model.**

- **Agent vs Run.** An *Agent* is a durable identity; a *Run* is one immutable prompt-to-terminal
  execution attempt (`RunRecord`) with an explicit state machine (`queued → running →
  completed|failed|cancelled|interrupted`, plus `waiting_for_input`/`waiting_for_approval`). State
  only advances, via compare-and-set.
- **Durable event log.** Every lifecycle transition emits exactly one event into an append-only log
  with a global monotonic `cursor` and per-aggregate sequence. Subscribers replay from a cursor and
  then follow live — so `GET /v1/events` is resumable (`Last-Event-ID`) and losslessly recoverable.
- **Interactions (human-in-the-loop).** A run can pause on a `question` or `approval`; answering it
  via `POST /v1/interactions/{id}/responses` resumes the run (an approval denial fails it).
- **Context packs.** A context runtime assembles the model's situational context from live providers
  (project instructions, transcript, git status, browser state, memory, todos, tools) under a
  deterministic budget, with sensitivity labels and reproducible digests. This path is wired into the
  server's run launcher.

**Live surface** (with `TABVIS_GATEWAY=1`): `/v1/runs`, `/v1/runs/{id}`, `/v1/runs/{id}/cancel`,
`/v1/conversations`, `/v1/interactions/{id}/responses`, `/v1/events` (SSE), and `/v1/gateway/health`.
The gateway runs in-process with its own authoritative SQLite store
(`browser-os-data/gateway.db`) and drains on shutdown; it reuses the legacy server's auth layer. A
legacy-compat adapter can project the classic `/agents` surface from Run data, and
`TABVIS_GATEWAY_AGENTS=1` cuts the legacy agent-lifecycle endpoints over to gateway Run data.

**Implementation status.** The gateway is built incrementally and additively — it does not replace
the legacy `tabvis/browser/server.py`, it mounts beside it. Current state:

| Area | Status |
|---|---|
| Contracts, Run split, durable events, cursor subscriptions | Implemented |
| Interactions (service + HTTP) | Implemented (model-resume-after-restart is future work) |
| Gateway extraction + real run execution, mounted in the daemon | Implemented |
| Context runtime | Implemented and wired into the run launcher |
| IM channels — 17 platforms + HTTP ingress + outbound delivery (below) | Implemented and **mounted** when `TABVIS_CHANNELS` is set |
| Plugin runtime, worker coordination, leased-binding browser path | Present as scaffolding, **not started/wired** in the daemon by default |

See [AGENT_GATEWAY_DESIGN.md](AGENT_GATEWAY_DESIGN.md) for the target architecture and
[DATA_MODEL.md](DATA_MODEL.md) for the on-disk records.

### IM channels

`tabvis/channels/` connects external messaging surfaces to the agent through one `ChannelPlugin`
contract (verify → normalize → deliver) and the same inbound pipeline (dedupe → bind → message event
→ Run). **17 IM platforms** ship, in two transport shapes:

- **Webhook** (an HTTP callback the plugin verifies): Feishu 飞书, DingTalk 钉钉, WeCom 企业微信, Slack,
  Microsoft Teams, LINE, Google Chat, WhatsApp, and QQ.
- **Client-loop** (a persistent connection that pushes into the pipeline): Telegram, Discord, Matrix,
  Mattermost, IRC, SimpleX, Signal, and iMessage.

Each verifies its platform's real scheme — Feishu/WeCom AES envelopes, Teams/Google Chat RS256 JWT, QQ
Ed25519, and the Slack/LINE/WhatsApp/DingTalk HMAC variants — and reads `TABVIS_<PLATFORM>_*` config.
Crypto/websocket channels pull an optional extra (`uv sync --extra feishu|wecom|teams|google_chat|qq|discord|mattermost|simplex`).

**Wired live.** Set `TABVIS_CHANNELS` (e.g. `feishu,slack,telegram`) under `tabvis --serve` and the
gateway mounts an ingress at `POST /v1/channels/<plugin>/webhook` (plus a GET handshake for WhatsApp),
starts the client-loop channels' read loops, and delivers each finished Run's reply back to its
originating chat by subscribing to `run.completed`. End-to-end: a chat message → webhook / read-loop →
verify → normalize → bind → **Run** → agent → reply delivered back to the chat.

Not included as channels: personal WeChat (个人微信 — no official bot API; automating a personal account
violates ToS; WeCom/企业微信 is the official enterprise path) and Tencent Yuanbao (腾讯元宝 — an assistant
app, not an IM). The `example_webhook` and `web` channels remain as the reference/console channels.
