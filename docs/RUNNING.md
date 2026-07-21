# Running Tabvis

A complete operational reference for the `tabvis` CLI, its configuration surface, the browser
runtime, and the local HTTP/SSE server. For *what* Tabvis can do (tools and subsystems), see
[FEATURES.md](FEATURES.md); for the control-plane design, see
[AGENT_GATEWAY_DESIGN.md](AGENT_GATEWAY_DESIGN.md).

> Tabvis is **headless**. There is no interactive terminal UI and no built-in web UI. You drive it
> with `-p/--print` (one-shot) or `--serve` (HTTP/SSE service), and optionally attach the React
> console with `--serve --dev`.

---

## 1. Requirements & install

- [uv](https://docs.astral.sh/uv/)
- Python 3.10+
- Node.js + npm — only if you want the web console (`--serve --dev` or a `web/` build)

```bash
uv sync                              # install Tabvis and its dependencies
uv run playwright install chromium   # download the default browser engine
```

Optional dependency groups (install only what you need):

| Extra | Install | Enables |
|---|---|---|
| `openai` | `uv sync --extra openai` | OpenAI / OpenAI-compatible providers (`TABVIS_MODEL_PROVIDER=openai`) |
| `gemini` | `uv sync --extra gemini` | Google Gemini provider (`TABVIS_MODEL_PROVIDER=gemini`) |
| `cloak` | `uv sync --extra cloak` | CloakBrowser stealth engine (downloads a patched Chromium on first launch) |
| `camoufox` | `uv sync --extra camoufox` | Camoufox stealth engine (downloads a patched Firefox on first launch) |
| `ocr` | `uv sync --extra ocr` | OCR fallback for text-only models (also works with a `tesseract` binary on PATH) |

---

## 2. Configure

```bash
cp .env.example .env
```

At minimum, set the model endpoint and a credential:

```dotenv
TABVIS_BASE_URL=https://api.anthropic.com
TABVIS_API_KEY=your-api-key
```

`TABVIS_BASE_URL` is **required** — Tabvis refuses to fall back to a default endpoint. Use
`TABVIS_AUTH_TOKEN` (a bearer/OAuth token) instead of `TABVIS_API_KEY` if your gateway needs it.

`.env.example` is the canonical, exhaustively-commented catalog of every setting. This document
summarizes the operationally important knobs; when in doubt, read `.env.example`.

---

## 3. The `tabvis` CLI

The entry point is `tabvis` (console script) or `python -m tabvis`. Argument parsing is a small
hand-rolled scanner (not argparse); a few flags are **positional** (noted below).

### 3.1 Invocation modes

| Mode | How | Behavior |
|---|---|---|
| **One-shot** | `tabvis -p "<goal>"` | Runs the agent loop once and exits with the result. This is the default. |
| **Server** | `tabvis --serve` | Starts the HTTP/SSE service (see §7). `--serve` must be the **first** argument. |
| **Dump prompt** | `tabvis --dump-system-prompt` | Prints the system prompt and exits. Must be the **first** argument. |
| **Version** | `tabvis --version` | Prints the version. Must be the **only** argument. |
| **No arguments** | `tabvis` | Prints headless-only guidance to stderr and exits `1`. It does **not** start an interactive UI. |

### 3.2 Flags

| Flag | Value | Default | Notes |
|---|---|---|---|
| `-p`, `--print` | prompt string | — | The goal to run. Presence of a prompt is what enables a one-shot run. `--print=<text>` inline form also works. |
| `--model` | model id | `claude-sonnet-4-6` | Main-loop model. Passed **verbatim** — CLI `--model` does **not** expand aliases (use `TABVIS_MODEL` for aliases like `sonnet`/`opus`/`haiku`). |
| `--browser-engine`, `--browser` | engine key | `chromium` | Sets `TABVIS_BROWSER_ENGINE` for the run. An unknown key exits `2` with the valid list. |
| `--output-format` | `text` \| `json` \| `stream-json` | `text` | See §3.3. |
| `--max-turns` | integer | none (unbounded) | Caps model turns; the run ends with an `error_max_turns` result if it hits the cap. |
| `--serve` | — | off | Start the server (first arg only). See §7. |
| `--host` | host | `127.0.0.1` | Server bind host (only read under `--serve`). |
| `--port` | integer | `8765` | Server bind port (only read under `--serve`). |
| `--dev` | — | off | Attach the live Vite web console (only under `--serve`). Equivalent to `TABVIS_WEB_DEV=1`. |
| `--dump-system-prompt` | — | — | Render the system prompt and exit (first arg only; honors a trailing `--model`). |
| `--bare` | — | off | Minimal prompt; skips MCP assembly and project instructions. Equivalent to `TABVIS_SIMPLE=1`. |
| `--version`, `-v`, `-V` | — | — | Print version (sole arg only). |

> **Positional gotcha:** because `--serve`, `--dump-system-prompt`, and `--version` are matched
> only in their required position, `tabvis --model x --serve` will *not* start the server — it falls
> through to the one-shot path with no prompt and exits `1`. Put `--serve` first:
> `tabvis --serve --host 0.0.0.0 --port 9000`.

### 3.3 Output formats (`--output-format`)

| Value | Emits |
|---|---|
| `text` (default) | Only the final assistant text, printed once the run completes. |
| `json` | The terminal `result` message as a single JSON object (fields: `type`, `subtype`, `is_error`, `result`, `num_turns`, `session_id`, `usage`, `stop_reason`, …). |
| `stream-json` | Every message as NDJSON (one JSON object per line) as the run streams, starting with a `system`/`init` frame. |

> **Scripting note:** a *task* failure (including `error_max_turns`) is reported **inside** the
> `result` message with `is_error: true` — the process still exits `0`. Only argument errors exit
> non-zero: `1` (no prompt / bad invocation), `2` (invalid `--browser-engine`).

### 3.4 Examples

```bash
uv run tabvis -p "summarize this repository"
uv run tabvis -p "open https://example.com and tell me what this page is for"
uv run tabvis -p "inspect this project, start the app, and verify the home page in the browser"
uv run tabvis -p "extract the pricing table" --output-format json
uv run tabvis -p "test this page in Firefox" --browser-engine firefox
uv run tabvis -p "long task" --max-turns 40
```

---

## 4. Configuration model

Tabvis reads configuration from several sources. Config is read **per operation** (not cached at
boot), so changing `os.environ` or `.env` takes effect on the *next* run — no restart needed.

### 4.1 `.env` autoloading & precedence

On startup Tabvis loads `.env` files with `override=False` (highest priority wins):

1. **Real process environment** — exported shell variables always win.
2. **Project `.env`** — `<cwd>/.env`.
3. **User `.env`** — `~/.tabvis/.env` (config home is `TABVIS_CONFIG_DIR` or `~/.tabvis`).

Related knobs:

- `TABVIS_DISABLE_DOTENV` — truthy disables `.env` autoloading entirely (must come from the real env).
- `TABVIS_DOTENV` — load one explicit `.env` file instead of the default project + user pair.

### 4.2 `settings.json`

Structured settings merge from these sources, low → high priority (later overrides earlier):

```
userSettings → projectSettings → localSettings
```

| Source | Path | Notes |
|---|---|---|
| `userSettings` | `~/.tabvis/settings.json` | Per-user |
| `projectSettings` | `<cwd>/.tabvis/settings.json` | Checked into the repo |
| `localSettings` | `<cwd>/.tabvis/settings.local.json` | Gitignored, machine-local |

Merge semantics: dicts deep-merge, lists concatenate + dedupe (first-seen order), scalars take the
highest-priority value. Enterprise/MDM policy sources are parsed but **not currently merged** into
effective settings in this build.

For a given knob the precedence is: **matching `TABVIS_*` env var > `settings.json` field > built-in
default** (e.g. `TABVIS_MODEL` > `settings.model` > `claude-sonnet-4-6`; `TABVIS_BROWSER_ENGINE` >
`settings.browserEngine` > `chromium`).

### 4.3 Project instructions (`TABVIS.md`)

Put durable repository guidance in `TABVIS.md`. Tabvis loads one file per directory from the Git root
down to the current working directory; files closer to the working directory take precedence.

| Limit | Value |
|---|---|
| Per file | 200 lines and 40,000 UTF-8 bytes |
| Total budget | 80,000 bytes, filled most-specific-first (broader files drop when the budget is exhausted) |

The format is plain Markdown — includes, frontmatter, and rule matching are intentionally
unsupported. Set `TABVIS_DISABLE_TABVIS_MDS=1` to disable loading. `--bare` / `TABVIS_SIMPLE` also
skip project instructions.

---

## 5. Model & provider configuration

### 5.1 Choosing a provider

Tabvis supports three providers behind one agent loop. The provider is resolved in this order:

1. `TABVIS_MODEL_PROVIDER` = `anthropic` | `openai` | `gemini` (forces the provider).
2. An explicit `provider/model` prefix (e.g. `openai/gpt-4o`).
3. Model-id inference: `gpt*`/`chatgpt`/`o1`/`o3`/`o4` → OpenAI; `gemini*` → Gemini.
4. Otherwise **Anthropic** (default).

A missing provider SDK raises a clear install hint (`uv sync --extra openai|gemini`) rather than
silently downgrading.

| Provider | Endpoint | Credential(s) | Extra |
|---|---|---|---|
| **Anthropic** (default) | `TABVIS_BASE_URL` (**required**) | `TABVIS_API_KEY` or `TABVIS_AUTH_TOKEN` | none |
| **OpenAI / compatible** | `TABVIS_OPENAI_BASE_URL` or `OPENAI_BASE_URL` | `TABVIS_OPENAI_API_KEY` or `OPENAI_API_KEY` | `openai` |
| **Gemini** | `TABVIS_GEMINI_BASE_URL` | `TABVIS_GEMINI_API_KEY` / `GEMINI_API_KEY` / `GOOGLE_API_KEY` | `gemini` |

The OpenAI provider speaks the Chat Completions API, so any OpenAI-compatible gateway (vLLM, Groq,
Together, a local server) works by pointing `TABVIS_OPENAI_BASE_URL` at it.

### 5.2 Model selection & aliases

The default model is `claude-sonnet-4-6`. Set `TABVIS_MODEL` to an id or a tier alias:

| Alias | Resolves to (tier default) |
|---|---|
| `sonnet`, `tabvis-balanced` | `TABVIS_DEFAULT_SONNET_MODEL` (default `claude-sonnet-4-6`) |
| `opus`, `tabvis-max`, `tabvis-plan` | `TABVIS_DEFAULT_OPUS_MODEL` (default `claude-opus-4-6`) |
| `haiku`, `tabvis-fast` | `TABVIS_DEFAULT_HAIKU_MODEL` (default `claude-haiku-4-5-20251001`) |

A `[1m]`/`[2m]` suffix requests an extended context window and is stripped before the API call.
Aliases resolve only via `TABVIS_MODEL`/`settings.model` — **not** via the `--model` CLI flag, which
is sent verbatim.

Other model knobs: `TABVIS_MAX_OUTPUT_TOKENS` (default `8192`), `TABVIS_MAX_RETRIES` (default `10`),
`TABVIS_STREAM_IDLE_TIMEOUT` (seconds to wait for the next stream chunk, default `90`),
`TABVIS_CUSTOM_HEADERS` (extra request headers, `Name: Value` per line).

### 5.3 Vision & OCR

Vision-capable models receive images directly. For text-only models, Tabvis OCRs image blocks to
text before the request (so browser screenshots still contribute).

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_MODEL_SUPPORTS_VISION` | auto-detect | `1` forces native-image, `0` forces the OCR path |
| `TABVIS_OCR_ENABLED` | `1` | OCR fallback on/off |
| `TABVIS_OCR_LANG` | `eng` | Tesseract language(s), e.g. `eng+chi_sim` |
| `TABVIS_OCR_ENGINE` | `auto` | `auto` \| `tesserocr` \| `pytesseract` \| `binary` |

The OCR layer tries `tesserocr` (in-process) → `pytesseract` → the `tesseract` binary, first that
works wins. Install the engine per OS (`brew install tesseract`, `apt install tesseract-ocr`,
`choco install tesseract`).

---

## 6. Browser configuration

The default engine is Playwright Chromium. Select another with `TABVIS_BROWSER_ENGINE` (or
`--browser-engine`).

### 6.1 Engines

| Category | `TABVIS_BROWSER_ENGINE` values | Profile | Notes |
|---|---|---|---|
| Playwright | `chromium` (default), `firefox`, `webkit` | Persistent, local | Bundled engines |
| Installed browsers | `chrome`, `msedge`, `brave`, `vivaldi`, `opera` | Persistent, local | Uses the system install (auto-detected binary or Playwright channel) |
| Stealth | `cloak` (CloakBrowser), `camoufox` | Persistent, local | Require the `cloak` / `camoufox` extra |
| Remote / connect | `cdp`, `connect`, `steel`, `browserless`, `browserbase`, and vendor profiles (`adspower`, `gologin`, `multilogin`, `octo`, `dolphin`, `kameleo`) | Remote-owned | Attach over CDP (`TABVIS_BROWSER_CDP_ENDPOINT`) or a Playwright ws endpoint (`TABVIS_BROWSER_WS_ENDPOINT`) |

Launch-based engines keep a **persistent profile** (cookies, logins, tabs) by default; each engine
uses its own profile directory, so switching engines starts logged out. Remote engines ride the
remote browser's own context.

> Engine keys are exact: Edge is `msedge` (not `edge`); the generic Playwright-server attach is
> `connect` (there is no engine literally named `remote`).

Install downloadable engines from the driver catalog:

```bash
uv run playwright install chromium        # or firefox / webkit / chrome / msedge
TABVIS_BROWSER_ENGINE=firefox uv run tabvis -p "test this page in Firefox"
```

### 6.2 Common browser options

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_BROWSER_HEADLESS` | `0` (headed) | Headless mode; auto-degrades to headless when no display is available |
| `TABVIS_BROWSER_ALLOWED_DOMAINS` | empty (allow all) | Comma list of allowed navigation hosts, exact or `*.example.com`; gates `BrowserNavigate` goto only |
| `TABVIS_BROWSER_VIEWPORT` | `1280x720` | Viewport `WIDTHxHEIGHT` |
| `TABVIS_BROWSER_TIMEOUT_MS` | `30000` | Default per-operation timeout |
| `TABVIS_BROWSER_USER_DATA_DIR` | per-engine | Persistent profile directory |
| `TABVIS_BROWSER_EAGER` | `1` | Pre-launch the browser at session start (`0` opts out) |
| `TABVIS_BROWSER_IDLE_TIMEOUT_MS` | `1800000` (30 min) | Reap an **unowned** idle workspace after this (`0` = never) |
| `TABVIS_BROWSER_AUTO_VISUAL` | `1` | Attach a screenshot + trimmed HTML when the accessibility snapshot is sparse |
| `TABVIS_BROWSER_ARGS` | empty | Extra Chromium launch args (comma-separated) |
| `TABVIS_BROWSER_CDP_ENDPOINT` | — | CDP address for `cdp`-mode engines |
| `TABVIS_BROWSER_WS_ENDPOINT` | — | Playwright-server ws endpoint for `connect`-mode engines |

### 6.3 Request pacing (good-network-citizen defaults)

Pacing is a **process-wide** limiter shared across concurrent agents; loopback hosts are never paced.

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS` | `1000` | Min gap between navigations/clicks to the same host (`0` disables per-host pacing) |
| `TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE` | `0` (off) | Per-host burst ceiling over a 60s window |
| `TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS` | `0` (off) | Min gap between *any* two browser actions |
| `TABVIS_BROWSER_REQUEST_JITTER_MS` | `0` | Random 0..N ms added per paced slot |
| `TABVIS_BROWSER_MAX_PACING_WAIT_MS` | `60000` | Safety cap on a single pacing wait |

### 6.4 Artifacts (browsing trail)

Recorded to `<session-dir>/browser-artifacts/` (`events.jsonl` + content-addressed DOM blobs) and
served via `GET /agents/<id>/artifacts`.

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_BROWSER_ARTIFACTS` | `1` | Record navigation/page/interaction/DOM events |
| `TABVIS_BROWSER_ARTIFACTS_DOM` | `1` | Capture page DOM per event |
| `TABVIS_BROWSER_ARTIFACTS_MAX_DOM_BYTES` | `1000000` | Cap per captured DOM blob |
| `TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT` | `0` | Store typed text as length-only |

### 6.5 Stealth options (`cloak` engine)

Read only when `TABVIS_BROWSER_ENGINE=cloak` (proxy/geoip/locale are also honored by `camoufox`;
timezone is cloak-only); inert otherwise.

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_BROWSER_PROXY` | — | Proxy URL (`http://`, `https://`, `socks5://`; inline creds are stripped from logs) |
| `TABVIS_BROWSER_HUMANIZE` | `0` | Human-like mouse/keystroke timing (adds latency) |
| `TABVIS_BROWSER_HUMAN_PRESET` | `default` | `default` \| `careful` |
| `TABVIS_BROWSER_GEOIP` | `0` | Derive timezone/locale from the proxy exit IP |
| `TABVIS_BROWSER_TIMEZONE` / `TABVIS_BROWSER_LOCALE` | host | IANA timezone / locale overrides |
| `TABVIS_BROWSER_CLOAK_LICENSE_KEY` | free tier | CloakBrowser Pro key (env-only; also honors `CLOAKBROWSER_LICENSE_KEY`) |

Downloads land in a per-run workspace (`<session-dir>/workspace/`, or an absolute
`TABVIS_WORKSPACE_DIR`); filenames are sanitized to a basename and de-collided so a hostile download
name cannot escape the directory.

---

## 7. The HTTP/SSE server (`--serve`)

```bash
uv run tabvis --serve                       # JSON/SSE API on http://127.0.0.1:8765
uv run tabvis --serve --host 0.0.0.0 --port 9000
```

- Default bind: `127.0.0.1:8765` (`TABVIS_SERVER_HOST` / `TABVIS_SERVER_PORT`, or `--host`/`--port`).
- Concurrency cap: `TABVIS_SERVER_MAX_AGENTS` (default `4`; each running agent is a real browser).
  Over-cap `POST /agent` returns `429`; a profile already bundled to another agent returns `409`.
- Run lifecycle is decoupled from the HTTP connection — a client disconnect does **not** kill a run;
  cancel explicitly via `POST /agents/{id}/cancel`.

### 7.1 Authentication

| Bind | Posture |
|---|---|
| Loopback (`127.0.0.1`, `::1`, `localhost`) | **Unauthenticated** — the caller is treated as a full local admin. |
| Non-loopback (`0.0.0.0`, `::`, a real host) | **Auth required.** The server refuses to start without `TABVIS_SERVER_ADMIN_TOKEN`. |

Credentials, when auth is engaged:

- `Authorization: Bearer <TABVIS_SERVER_ADMIN_TOKEN>` → admin.
- `x-tabvis-agent-credential: <token>` → that agent's principal (the `agent_id` comes from the
  credential, never the request body). Mint one via `POST /agents/register`.

Transport hardening is always on: security headers, a body-size cap
(`TABVIS_SERVER_MAX_BODY_BYTES`, default 10 MiB → `413`), and default-deny CORS
(`TABVIS_SERVER_CORS_ORIGINS` to allow origins). `POST /config` and `POST /browsers/install` are
**loopback-only** unless `TABVIS_SERVER_ALLOW_REMOTE_CONFIG=1`.

### 7.2 Endpoints

Every legacy API route below is also mounted under a `/v1` alias with an identical handler (the
console `/` is intentionally not versioned).

**Health & config**

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Fleet status: running/capacity, agents, browsers, config readiness. Use this as the liveness probe. |
| GET | `/config` | Current editable settings (secrets report set/hint only). |
| POST | `/config` | Apply + persist settings live (loopback-only); merged into `.env`. Body `{"values": {KEY: val}}`. |
| GET | `/` | **Not** a console by default — returns a `404` JSON pointer. Under `--dev` it reverse-proxies the Vite console. |

**Agents (runs)**

| Method | Path | Purpose |
|---|---|---|
| POST | `/agent`, `/agents` | Run one agent (SSE stream); response header `X-Agent-Id`. Body `{prompt, agent_id?, model?, max_turns?, profile?, stream?}`. |
| GET | `/agents` | List agent runs (`?status=&limit=`). |
| GET | `/agents/{id}` | One agent's full record + live browser view. |
| POST | `/agents/{id}/cancel` | Stop a running agent. |
| POST | `/agents/{id}/quit` | Quit the agent and close its bundled browser (frees the profile). |
| GET | `/agents/{id}/browser` | The agent's browser-session record. |
| GET | `/agents/{id}/artifacts` | Browsing trail (`?dom=<ref>` fetches one DOM blob, `?limit=N`). |
| POST | `/agents/register` | Register an agent and mint a local credential. |
| GET | `/agents/{id}/identity` | Browser identity metadata (refs only). |

**Browsers & workspaces**

| Method | Path | Purpose |
|---|---|---|
| GET | `/browsers` | Persistent browser workspaces still open. |
| GET | `/browsers/drivers` | Driver catalog + install state. |
| POST | `/browsers/install` | Install a Playwright browser (SSE progress; loopback-only). |
| POST | `/browsers/close` | Close a workspace by `user_data_dir`/`profile`/`agent_id`. |
| GET | `/workspaces`, `/workspaces/{id}/snapshot` | First-class workspace snapshots. |
| POST | `/workspaces/{id}/pause`, `/workspaces/{id}/close` | Workspace lifecycle. |
| WS | `/v1/events` | Semantic observation events over WebSocket (silent unless `TABVIS_BROWSER_EVENT_BUS=1`). |

**SSE frames** on `POST /agent`: `agent`, `system`, `assistant`, `tool_use`, `tool_result`,
`delta` (when `stream:true`), `result`, `error`/`cancelled`, and a final `done`. Image bytes are
elided from the stream.

### 7.3 Gateway control plane

With `TABVIS_GATEWAY` enabled (**the default**), the Agent Gateway mounts these gateway-native routes
alongside the legacy surface. Disable with `TABVIS_GATEWAY=0`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/gateway/health` | Gateway component readiness + capacity (`200` ready/degraded, `503` otherwise). |
| POST | `/v1/conversations` | Create a conversation. |
| POST | `/v1/runs` | Create + start a Run (`202 Accepted`). |
| GET | `/v1/runs/{id}` | Read a Run. |
| POST | `/v1/runs/{id}/cancel` | Cancel a Run. |
| POST | `/v1/interactions/{id}/responses` | Answer a pending question/approval and resume the Run. |
| GET | `/v1/events` | Cursor-resumable SSE event subscription (`?cursor=`, `?run_id=`, `?follow=0/1`; `Last-Event-ID` resume). |

The gateway runs **in-process** with its own authoritative SQLite store
(`<config-home>/browser-os-data/gateway.db`) and drains on server shutdown. It reuses the same auth
layer as the legacy server. See [AGENT_GATEWAY_DESIGN.md](AGENT_GATEWAY_DESIGN.md) and
[FEATURES.md](FEATURES.md#agent-gateway) for the model and current implementation status.

A second, off-by-default flag `TABVIS_GATEWAY_AGENTS=1` re-backs the legacy `/agent`, `/agents`,
`/agents/{id}` GET, and `/agents/{id}/cancel` endpoints with gateway Run data (registry-retirement
cutover); the browser-bundle endpoints stay registry-backed either way.

### 7.4 The web console

Tabvis serves no UI by default. Two ways to get the console:

**A — live dev console (recommended for local use)**

```bash
uv run tabvis --serve --dev        # or  TABVIS_WEB_DEV=1 uv run tabvis --serve
```

The Python server starts Vite (`npm run dev` in `web/`) and reverse-proxies the console to it, so the
whole app is live at `http://127.0.0.1:8765/` on one origin with hot-reload. Requires Node/npm and
`web/node_modules` (`cd web && npm install`); it fails loudly without them. HMR connects straight to
Vite on `:5173`.

**B — self-hosted static build**

```bash
cd web && npm run build            # bundles to web/dist/
```

Serve `web/dist/` from any static host and make sure its API calls reach a running Tabvis server —
simplest is the same origin behind a reverse proxy. See [`web/README.md`](../web/README.md).

Dev-mode knobs: `TABVIS_WEB_DIR` (override the `web/` location), `TABVIS_WEB_DEV_HOST`/
`TABVIS_WEB_DEV_PORT` (Vite bind, default `127.0.0.1:5173`), `TABVIS_SERVER` (Vite-standalone API
proxy target, default `http://127.0.0.1:8765`).

---

## 8. Environment variable reference

`.env.example` is the authoritative catalog. The tables above cover model, browser, and server
knobs; the categories below list the remaining operationally useful variables. Most of the model/
browser/server settings are also editable at runtime through `POST /config` (and the web console's
Settings page).

### 8.1 Server & gateway

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_SERVER_HOST` / `TABVIS_SERVER_PORT` | `127.0.0.1` / `8765` | Bind address |
| `TABVIS_SERVER_MAX_AGENTS` | `4` | Concurrent-agent cap |
| `TABVIS_SERVER_ADMIN_TOKEN` | — | Admin bearer token (required for non-loopback binds) |
| `TABVIS_SERVER_CORS_ORIGINS` | — | Allowed CORS origins (comma list, or `*`) |
| `TABVIS_SERVER_MAX_BODY_BYTES` | `10485760` | Max request body |
| `TABVIS_SERVER_ALLOW_REMOTE_CONFIG` | off | Allow `POST /config` / install from non-loopback |
| `TABVIS_GATEWAY` | **on** | Mount the gateway control plane (`0`/`false`/`no`/`off` disable) |
| `TABVIS_GATEWAY_AGENTS` | off | Serve legacy `/agents` from gateway Run data |
| `TABVIS_WEB_DEV` | off | Attach the Vite console (= `--dev`) |

### 8.2 Permissions & policy

None of these appear in `.env.example` yet; they are set in the environment or `settings.json`.

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_PERMISSION_MODE` | `standard` | `trusted` \| `standard` \| `locked` (an invalid value is a config error) |
| `TABVIS_PERMISSION_SHADOW` | off | Compute + audit decisions but never block (audit-only) |
| `TABVIS_PERMISSION_AUDIT` | **on** | `policy.decision` audit trail (falsy to disable) |
| `TABVIS_PERMISSION_FS_STRICT` | off | Strict filesystem baseline (`fs:` writes need a grant, secret reads denied) |
| `TABVIS_PERMISSION_BASH_STRICT` | off | Network-touching bash commands fall to `ask` |

See [FEATURES.md](FEATURES.md#permissions--policy) for the full model.

### 8.3 Paths

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_CONFIG_DIR` | `~/.tabvis` | Config home (settings, browser profiles, agent/gateway records) |
| `TABVIS_WORKSPACE_DIR` | per-session | Absolute download/workspace directory override |
| `TABVIS_TMPDIR` | OS default | Base temp directory |
| `TABVIS_MEMORY_PATH_OVERRIDE` | derived | Override the auto-memory directory |

### 8.4 Agent runtime, memory & context

| Var | Default | Meaning |
|---|---|---|
| `TABVIS_SIMPLE` | off | Minimal prompt, reduced tools (`Bash`/`Read`/`Edit`), no MCP, no project instructions (= `--bare`) |
| `TABVIS_MCP_CONFIG` | — | Inline MCP server config (JSON document, or a path to one) |
| `TABVIS_HOOKS` | — | Inline hooks config |
| `TABVIS_DISABLE_AUTO_MEMORY` | off | Disable persistent file-based memory |
| `TABVIS_DISABLE_1M_CONTEXT` | off | Disable the 1M-token context window for `[1m]` models |
| `TABVIS_AUTOCOMPACT_PCT_OVERRIDE` | — | Override the auto-compaction trigger percentage |
| `TABVIS_BROWSER_EVENT_BUS` | off | Enable observation events (`observation` SSE frames + `WS /v1/events`) |
| `TABVIS_BROWSER_INTENTS` | off | Expose the flag-gated `BrowserIntent` tool |

### 8.5 Debugging

| Var | Meaning |
|---|---|
| `TABVIS_DEBUG` | Debug logging (**presence-checked** — even `TABVIS_DEBUG=0` enables it) |
| `TABVIS_LOG` | Anthropic SDK HTTP logging (e.g. `debug`; mirrored to `ANTHROPIC_LOG`) |
| `TABVIS_OVERRIDE_DATE` | Override the "today" date the agent sees |
| `TABVIS_DIAGNOSTICS_FILE` | Write a diagnostics log to this path |

---

## 9. Development

```bash
uv sync
uv run pytest -q
uv run python -m compileall -q tabvis
uv run tabvis --version
```

The web console has its own toolchain — see [`web/README.md`](../web/README.md).

---

## 10. Troubleshooting

**`RuntimeError: TABVIS_BASE_URL is required`** — set `TABVIS_BASE_URL` in `.env`. Tabvis refuses to
use a default model endpoint.

**`tabvis` prints guidance and exits 1** — you ran it with no prompt. Use `-p "<goal>"`, or `--serve`.
Remember `--serve`/`--version` are positional (put `--serve` first).

**`GET /` returns 404 with no console** — expected. Tabvis serves no UI by default; run
`--serve --dev` or host a `web/` build (§7.4). Health-check `/health`, not `/`.

**`--serve --dev` fails to start** — the dev console needs Node/npm and `web/node_modules`. Run
`cd web && npm install`. Override the source dir with `TABVIS_WEB_DIR`.

**`POST /agent` returns 429** — at capacity. Raise `TABVIS_SERVER_MAX_AGENTS` (each agent is a real
browser, so mind memory) or wait for a run to finish. `409` means the browser profile is already
bundled to another agent — use a different `profile`/`agent_id`.

**Server won't start on a public host** — a non-loopback bind requires `TABVIS_SERVER_ADMIN_TOKEN`.
Set it, or bind `127.0.0.1` and front the server with your own proxy.

**A navigation is blocked / asked** — `TABVIS_BROWSER_ALLOWED_DOMAINS` is set and the host doesn't
match. In headless one-shot mode an `ask` resolves to *deny*. Add the host (exact or `*.example.com`)
or clear the allowlist to allow all.

**A stealth engine won't launch** — install the extra (`uv sync --extra cloak` / `--extra camoufox`);
the first launch downloads a patched browser (~140 MB for cloak).

**Images aren't understood by a text-only model** — install an OCR engine (`uv sync --extra ocr`, or a
`tesseract` binary) and confirm `TABVIS_OCR_ENABLED=1`.

**A model provider errors on import** — install its extra (`uv sync --extra openai|gemini`) and set the
provider's key/base-URL env vars (§5.1).
