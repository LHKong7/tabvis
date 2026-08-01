# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

Tabvis is a browser-native AI agent: one reasoning loop that drives a real Playwright browser *and*
edits files / runs shell / calls MCP tools. It runs as a non-interactive one-shot CLI (`-p`) or as a
local Web console plus HTTP/SSE service (`tabvis` / `--serve`). There is no interactive terminal UI.

## Commands

Python (uv-managed, Python 3.10+; repo pins 3.13):

```bash
uv sync                                   # install deps
uv run playwright install chromium        # download the default browser engine
uv run tabvis -p "summarize this repo"    # one-shot agent run
uv run tabvis                             # Web console + HTTP/SSE API on 127.0.0.1:8765
uv run tabvis --serve                     # explicit equivalent of the command above

uv run pytest -q                          # full suite
uv run pytest tests/gateway -q            # one directory
uv run pytest tests/gateway/test_run_store.py::test_create_run_persists_and_emits_run_created -q   # single test
uv run ruff check <paths>                 # lint;  add --fix to auto-fix (removes unused imports etc.)
uv run python -m compileall -q tabvis     # fast syntax check without running
```

Optional feature extras (installed on demand, never silently required — a missing package raises a
clear install hint): `uv sync --extra <name>` where name ∈ `cloak camoufox` (stealth browsers),
`openai gemini` (model providers), `ocr` (text-only-model image OCR), and the IM channels needing
crypto/websockets: `feishu wecom teams google_chat qq discord mattermost simplex`.

Web console (Vite/React source; the production build ships inside the Python package):

```bash
cd web && npm install
uv run tabvis --serve --dev               # server starts Vite + reverse-proxies the console at :8765/ (HMR via :5173)
cd web && npm run build                   # production bundle -> tabvis/browser/static/
```

## Configuration model

Config is read **per operation** (not cached at boot), so changing env or `.env` takes effect on the
next run. Precedence for a given knob: matching `TABVIS_*` env var > `settings.json` field > built-in
default. `.env` autoloads from `<cwd>/.env` then `~/.tabvis/.env` (real shell env always wins).
`.env.example` is the exhaustive, commented catalog of every setting.

- **`TABVIS_BASE_URL` is required** — Tabvis refuses to run without a model endpoint. Auth via
  `TABVIS_API_KEY` or `TABVIS_AUTH_TOKEN`. A bare `ANTHROPIC_API_KEY` is *deleted* at startup; use the
  `TABVIS_*` name. Provider inferred from the model id or forced by `TABVIS_MODEL_PROVIDER`.
- Durable project instructions go in `TABVIS.md` files (loaded git-root → cwd, nested wins; plain
  Markdown, no includes/frontmatter). `~/.tabvis` (or `TABVIS_CONFIG_DIR`) is the config home for
  settings, browser profiles, and the SQLite stores.

## Architecture (the big picture)

Package layout under `tabvis/`: `agent/` (loop, providers, tools, MCP, skills, workflows, memory,
sub-agents), `browser/` (runtime, sessions, downloads, artifacts, the HTTP/SSE server), `gateway/`
(the control plane), `channels/` (IM integrations), `policy/` (permission engine), `ui/` (CLI
entrypoints, config-over-HTTP, slash commands), plus `constants/`, `state/`, `types/`, `utils/`.

**The agent loop** (`agent/query/`, `agent/query_engine.py`). Each turn: auto-compaction (fail-open,
before the model call) → model stream → extract `tool_use` blocks → no tools means the run completes
→ else run tools, append results, repeat. `--max-turns` is unbounded by default. The tool registry
is `agent/tools/` (20 built-in singletons via `get_all_base_tools()`); connected MCP tools join the
same pool at runtime and are *deferred* (schemas withheld until `ToolSearch` loads them). Providers
(`agent/api/providers/`) translate OpenAI/Gemini streaming into Anthropic-style parts so the loop is
provider-blind. `TABVIS_SIMPLE`/`--bare` reduces the registry to `Bash`/`Read`/`Edit` and skips MCP.

**Browser subsystem** (`browser/`). Tabvis perceives pages as **accessibility snapshots** where each
element is tagged `[ref=eN]`; tools act on refs, and every action returns a fresh snapshot (a stale
ref raises a recoverable "snapshot again" error). There is **no `WebFetch` tool** — the browser is the
web interface. An agent owns one persistent browser workspace keyed by a profile (1:1, so parallel
agents need different profiles). All browser tool calls route through the single Policy Guard
(`browser/policy_guard.py`). Request pacing (`browser/rate_limiter.py`) is process-wide and on by
default.

**The Agent Gateway** (`gateway/`) is the durable control plane and is **authoritative for the
`/agents` surface** (a recent convergence retired the legacy `AgentRecord` registry from the public
path — do not reintroduce it there). Core model:
- **Durable Agent** (`runtime/agents.py`, the `agents` table) — identity/config/lifecycle
  (`active/disabled/deleted`) that outlives its runs. Created/refreshed **inside** `RunStore.create_run`'s
  transaction, atomic with the run.
- **Immutable Run** (`runtime/runs.py`, `run_store.py`) — one prompt-to-terminal execution with an
  11-state machine and compare-and-set transitions. **Invariant**: a run row and its `run.created`
  event commit in one `db.transaction()`; state only advances via `apply_transition(expected=…)`.
- **Durable event log** (`events/store.py`) — append-only, global monotonic `cursor` + per-aggregate
  `seq`. Live fan-out is the in-memory `LiveBus` (`events/subscriptions.py`), non-authoritative;
  recovery is replay-by-cursor. `EventStore.append(conn=…)` joins a caller's transaction; the caller
  calls `notify_live` after commit.
- **Legacy `/agents` = "durable Agent + latest Run"**: `protocol/compatibility.py` projects a Run to
  the legacy agent shape and additively merges the durable Agent. **Preservation contracts** to keep:
  the 20-key `/agents` body, the 5-value status vocab (`queued|running|completed|failed|cancelled` —
  the 11 run states map *down* to these), SSE frame names (`agent|assistant|tool_use|result|done|
  cancelled|error`), and the `X-Agent-Id` header. Legacy `AgentRecord` envelopes are migrated into the
  gateway at startup by `runtime/legacy_migration.py` (idempotent, forward-only).
- The gateway mounts alongside the legacy server (`browser/server.py`); `TABVIS_GATEWAY=0` only gates
  the extra `/v1` control-plane routes, not the `/agents` backing.

**Two SQLite stores**, both under `<config-home>/browser-os-data/`: `gateway.db` (`gateway/store/db.py`,
the **authoritative** control plane; `SCHEMA_VERSION`, forward-only, additive-tables-only `_migrate`
with **no `ALTER TABLE`** path) and the legacy `runtime.db` (`browser/persistence/db.py`, a best-effort
shadow of the file-based sources of truth). Each table keeps queryable columns plus a `data` JSON blob
for a lossless round-trip — adding a record *field* needs no schema change; adding a *column* does (and
isn't supported by the migrator), so prefer new tables or blob fields.

**IM channels** (`channels/`). One `ChannelPlugin` contract (`core/contract.py`: `normalize`/`deliver`
/lifecycle) funnels 17 platforms through the same inbound pipeline (dedupe → bind → event → Run) and
delivery path. Two transport shapes: **webhook** (verify an HTTP callback, hand a `RawInbound` to
`ChannelGateway.receive_webhook`) and **client-loop** (`plugins/_platform/loop.py` — a persistent
connection pushes into the pipeline). Each plugin declares `signed_webhooks=False` and verifies its
own scheme. `gateway/runtime/channels.py` (`ChannelRuntime`) mounts configured plugins when
`TABVIS_CHANNELS` is set: inbound at `POST /v1/channels/<plugin>/webhook`, and it delivers a finished
Run's result back to its originating chat by subscribing to `run.completed`. Shared bases live in
`plugins/_platform/`; `plugins/feishu/` is the reference implementation.

**Policy engine** (`policy/`). Actions are classified by side-effect category (`filesystem.write`,
`shell.execute`, `browser.navigate`, …), not tool name, and checked against rules yielding
`allow`/`deny`/`ask` merged low→high: mode baseline → `settings.json` rules → identity → grants (a
`deny` always wins). Modes: `trusted`/`standard`(default)/`locked`. Wired into Bash, FileWrite/Edit,
the Browser tools, and the runtime API. In headless `-p` runs an `ask` resolves to *deny*. The OS
sandbox is **not bundled** (`utils/sandbox/` is a stub); real confinement is working-directory
restriction with symlink-aware path resolution.

## Testing conventions

`tests/conftest.py` pins `TABVIS_CONFIG_DIR` to a fresh tmp dir per test (so each test gets its own
`gateway.db`/`runtime.db`; the DB connections reopen on a config-home change). `tests/gateway/conftest.py`
additionally resets the gateway's process-global singletons (event store, run store, agent store,
orchestrator, live bus) between tests — when you add a new gateway singleton, reset it there too. Tests
run against the real Starlette app via `TestClient` and drive the gateway/channel stores directly; no
network or model is needed (launchers/streams are injectable seams).
