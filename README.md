<p align="center">
  <img src="assets/tabvis-icon.png" width="128" height="128" alt="Tabvis product icon">
</p>

<h1 align="center">Tabvis</h1>

<p align="center">
  <strong>A browser-native AI agent for real work on the web and in your workspace.</strong>
</p>

<p align="center">
  Give Tabvis an outcome. It can inspect a project, edit files, run commands, open a real browser,
  operate JavaScript applications, and keep working until the task is complete.
</p>

---

## What is Tabvis?

Tabvis is a headless agent runtime built around one idea: an agent should be able to work across
your local project and the live web in the same reasoning loop.

Instead of fetching simplified page text, Tabvis drives a real Playwright browser. It sees an
accessibility snapshot of the current page, acts on stable element references, and observes the
result after every click, keystroke, or navigation. At the same time it can search code, edit
files, run shell commands, call MCP tools, and delegate focused work to sub-agents.

You can run Tabvis as a one-shot CLI agent or as a local HTTP/SSE service with a web console.

## Why Tabvis?

| | Product capability |
|---|---|
| **Browser-native** | Drives Chromium, Firefox, WebKit, installed browsers, stealth engines, or remote browser sessions through one tool interface. |
| **Code + web in one loop** | Reads and changes a project, executes commands, and validates results in the browser without switching agents. |
| **Real browser state** | Persistent profiles retain cookies, logins, tabs, and session context across runs. |
| **Observable automation** | Headful mode lets you watch the agent work; snapshots, browser history, DOM captures, and event streams make runs inspectable. |
| **Automation-ready** | Use one-shot CLI output for scripts, or launch concurrent agents through the HTTP/SSE API. |
| **Extensible** | Connect MCP servers, load project/user skills, select model providers, and compose multi-agent workflows. |
| **Policy-aware** | File, shell, and browser actions pass through permission and policy layers before execution. |

## What can you build with it?

- Research a topic across JavaScript-heavy or authenticated websites.
- Operate internal dashboards and web applications using an existing login.
- Change a feature, start the local app, and verify the result in a browser.
- Collect structured information from multi-step pages without relying on brittle selectors.
- Run browser tasks from CI, a local service, or another application over SSE.
- Connect private tools and data through MCP, then use them alongside browser and coding tools.
- Split a larger goal into scoped sub-agent or workflow tasks.

## How it works

```text
Your goal
   │
   ▼
Agent reasoning loop
   ├── Files:      Read · Edit · Write · Glob · Grep · NotebookEdit
   ├── Runtime:    Bash · Workflow · Agent · TodoWrite · AskUserQuestion · ToolSearch
   ├── Browser:    BrowserNavigate · BrowserSnapshot · BrowserClick · BrowserType · BrowserWait · BrowserDownload
   └── Extensions: Skill (project/user skills) · MCP tools
   │
   ▼
Observed result → next action → completed outcome
```

The browser is Tabvis's web interface. There is no separate `WebFetch` shortcut: pages are rendered
with JavaScript, actions happen in a real browser context, and every browser action returns a new
observation for the model.

## Quick start

### Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.10+

### Install

```bash
uv sync
uv run playwright install chromium
```

### Configure

```bash
cp .env.example .env
```

Set these values in `.env`:

```dotenv
TABVIS_BASE_URL=https://api.anthropic.com
TABVIS_API_KEY=your-api-key
```

`TABVIS_AUTH_TOKEN` may be used instead of `TABVIS_API_KEY`. OpenAI-compatible and Gemini providers
are also supported through optional dependencies and their corresponding environment settings.

### Project instructions

Put durable repository guidance in `TABVIS.md`. Tabvis loads one file per directory from the Git
root to the current working directory; files closer to the working directory take precedence.
Each file is limited to 200 lines and 40,000 UTF-8 bytes, with an 80,000-byte total budget that
prioritizes the most specific files. The format is plain Markdown—includes, frontmatter, and rule
matching are intentionally unsupported. Set `TABVIS_DISABLE_TABVIS_MDS=1` to disable loading.

### Run your first task

```bash
uv run tabvis -p "open https://example.com and tell me what this page is for"
```

Try a code-and-browser task:

```bash
uv run tabvis -p "inspect this project, start the app, and verify the home page in the browser"
```

## Two ways to use Tabvis

### One-shot CLI

The CLI accepts a goal, runs the agent loop, and exits with the result.

```bash
uv run tabvis -p "summarize this repository"
uv run tabvis -p "..." --model claude-sonnet-4-6
uv run tabvis -p "..." --output-format json
uv run tabvis -p "..." --max-turns 20
```

Tabvis is intentionally headless: without `-p/--print`, it prints usage guidance rather than
starting an interactive terminal UI.

### Local agent service

Run the HTTP/SSE server:

```bash
uv run tabvis --serve
# JSON/SSE API on http://127.0.0.1:8765
```

Tabvis is headless and serves **no built-in UI** by default — `GET /` returns a JSON pointer, and
`/health` is the liveness probe. To use the bundled web console, either attach the live dev console:

```bash
uv run tabvis --serve --dev     # starts Vite and reverse-proxies the console at http://127.0.0.1:8765/
```

or build `web/` and host the static bundle yourself, pointed at the API (see [`web/README.md`](web/README.md)).

Launch an agent programmatically over SSE:

```bash
curl -N -X POST http://127.0.0.1:8765/agent \
  -H 'content-type: application/json' \
  -d '{"prompt":"open example.com and return the main heading"}'
```

The service can launch and inspect agents, stream events, cancel runs, expose browser history and
artifacts, and update supported settings without restarting the process. Agents run in parallel with
isolated browser workspaces. With `TABVIS_GATEWAY` enabled (the default), the gateway control-plane
routes (`/v1/runs`, `/v1/events`, `/v1/interactions/…`, `/v1/gateway/health`) mount alongside the
legacy surface. See [Running Tabvis](docs/RUNNING.md) for the full endpoint reference.

> **Security.** On its default `127.0.0.1` bind the server is unauthenticated — anyone who can reach
> the port acts as you. A token-based auth layer (admin bearer token + per-agent credentials) engages
> automatically on any non-loopback bind, and the server refuses to start a public bind without
> `TABVIS_SERVER_ADMIN_TOKEN`. Even so, do not expose it to an untrusted network without a proxy and
> appropriate isolation.

## Browser choices

The default engine is Playwright Chromium. The same Tabvis browser tools also work with:

- Playwright Firefox and WebKit
- Installed Chrome, Edge, Brave, Vivaldi, or Opera
- CloakBrowser and Camoufox stealth engines
- Existing browsers over CDP
- Remote Playwright, Browserless, Browserbase, and compatible services

```bash
TABVIS_BROWSER_ENGINE=firefox uv run tabvis -p "test this page in Firefox"
TABVIS_BROWSER_HEADLESS=1 uv run tabvis -p "run this browser task in CI"
TABVIS_BROWSER_ALLOWED_DOMAINS="example.com,*.example.com" uv run tabvis -p "..."
```

Launch-based engines use persistent profiles by default. Remote engines use the session managed
by the remote browser provider.

## Product architecture

```text
tabvis/
├── agent/       reasoning loop, model providers, tools, memory, MCP, skills, workflows, sub-agents
├── browser/     browser runtime, sessions, downloads, artifacts, HTTP/SSE service
├── gateway/     Agent Gateway control plane — runs, durable events, interactions, context packs
├── channels/    inbound/outbound messaging framework (web + webhook plugins)
├── policy/      unified permission engine — actions, resources, modes, adapters
├── ui/          CLI entrypoints, server config API, slash commands, workflows
├── services/    shared runtime services
├── constants/   tool names and shared constants
├── state/       app/session state
├── types/       shared and generated types
└── utils/       settings, model resolution, permissions, sandbox, and other helpers
```

The built-in tool registry lives in `tabvis/agent/tools/`. Connected MCP tools are added to the same
tool pool at runtime. Anthropic is the default model protocol; OpenAI and Gemini providers adapt
their native streaming formats to the same agent loop.

The **Agent Gateway** (`tabvis/gateway/`) is a control plane that fronts the runtime: a single command
ingress, a durable append-only event log, and an explicit split between a durable *agent* and an
immutable *run* (one prompt-to-terminal execution). It mounts alongside the legacy server by default
(`TABVIS_GATEWAY=1`), exposing `/v1/runs`, `/v1/events` (SSE), `/v1/interactions/…`, and
`/v1/gateway/health`. See the [Agent Gateway design](docs/AGENT_GATEWAY_DESIGN.md) for the full model.

## Documentation

| Document | Use it for |
|---|---|
| [Running Tabvis](docs/RUNNING.md) | Complete CLI, environment, browser, server, settings, and troubleshooting reference. |
| [Feature overview](docs/FEATURES.md) | Current tool surface and subsystem behavior. |
| [Data model](docs/DATA_MODEL.md) | On-disk records, ID scheme, and the SQLite stores. |
| [Agent Gateway design](docs/AGENT_GATEWAY_DESIGN.md) | Control-plane architecture — channels, runs, interactions, context — and the incremental migration plan. |
| [Web console](web/README.md) | Running, developing, and self-hosting the React console. |

## Development

```bash
uv sync
uv run pytest -q
uv run python -m compileall -q tabvis
uv run tabvis --version
```

Tabvis is under active development. See `.env.example` for the complete configuration surface and
[docs/](docs/) for architecture and reference material.

## Legal notice & responsible use

> ⚠️ Tabvis drives a real browser, can operate stealth engines, and can download files — how you use
> it is **your responsibility**. By using Tabvis you agree to the following.

- **Authorized use only.** Only automate, access, scrape, or download from systems you own or are
  explicitly authorized to use. Respect each site's Terms of Service, `robots.txt`, rate limits, and
  all applicable laws — including computer-misuse / unauthorized-access, copyright, contract, and
  data-protection / privacy law in the relevant jurisdictions.

- **Anti-detection features are for legitimate, authorized work.** The stealth browsers
  (CloakBrowser / Camoufox), fingerprint and behavior options, and proxy support exist for uses such
  as testing **your own** bot defenses, authorized security assessments, QA, accessibility, and
  research. Do **not** use them to defeat CAPTCHAs or human-verification, circumvent access controls
  or security measures, gain unauthorized access, create fake accounts, or commit fraud. Tabvis does
  not include automated CAPTCHA / anti-bot **solving**, and requests to add it will not be honored.

- **Be a good network citizen.** Request pacing is on by default
  (`TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS` and related knobs) so the agent does not burst a server.
  You remain responsible for staying within each site's limits and for not causing disruption or a
  denial of service — do not raise the limits to hammer a third party.

- **Downloaded content.** Files the agent fetches into the workspace may be copyrighted, private, or
  otherwise restricted. You are responsible for the lawful handling, storage, and use of anything it
  downloads or reads.

- **Credentials & exposure.** On its default `127.0.0.1` bind the local server is **unauthenticated** —
  anyone who can reach the port acts as you. Token auth engages on non-loopback binds (and the server
  refuses to start a public bind without `TABVIS_SERVER_ADMIN_TOKEN`), but you should still front it
  with an authentication proxy and isolation before exposing it. Keep API keys and browser profiles /
  cookies secure.

- **No warranty.** Tabvis is provided "as is", without warranty of any kind, and its authors and
  contributors accept no liability for how it is used or for any resulting damages. This notice is
  not legal advice; if you are unsure whether a use is permitted, obtain the target operator's
  permission and seek your own counsel first.

## License

Tabvis is released under the [MIT License](LICENSE).
