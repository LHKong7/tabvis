// API response shapes for the tabvis server (tabvis/browser/server.py + ui/entry/config_api.py).
// Only the fields the console consumes are typed; loose/optional to tolerate server additions.

export interface EngineInfo {
  key: string
  label: string
  mode: string // launch | plugin | cdp | connect
  kernel?: string
  browser_type?: string
  stealth?: boolean
  requires?: string // optional package name, e.g. "cloakbrowser"
  notes?: string
}

export interface HealthConfig {
  ready?: boolean
  missing?: string[]
  base_url?: string
  credential?: boolean
  model?: string
  playwright?: boolean
  browser_headless?: boolean
  max_agents?: number
  browser_profile_dir?: string
  browser_engine?: string
  browser_engines?: EngineInfo[]
  engine_ready?: boolean
  engine_package_ready?: boolean
  engine_endpoint_ready?: boolean
  browser_kernel?: string
  browser_type?: string
  browser_mode?: string
  browser_cdp_endpoint_set?: boolean
  browser_ws_endpoint_set?: boolean
  cloak_licensed?: boolean
}

export interface Health {
  status: string
  running: number
  max_agents: number
  capacity: number
  agents: number
  browsers: number
  config: HealthConfig
}

export type SettingKind = 'text' | 'secret' | 'bool' | 'number'

export interface Setting {
  key: string
  label: string
  group: string
  kind: SettingKind
  help?: string
  placeholder?: string
  value?: string
  set?: boolean
  hint?: string
}

export interface ConfigResponse {
  settings: Setting[]
  writable: boolean
  env_file: string
}

export interface AgentSummary {
  agent_id: string
  status: string
  prompt: string
  turns: number
  tool_calls: number
  duration_ms?: number
  profile?: string
}

export interface BrowserInfo {
  status?: string
  engine?: string
  version?: string
  headless?: boolean
  profile_dir?: string
  stealth?: boolean
}

export interface HistoryEntry {
  url: string
  title?: string
}

export interface BrowserView {
  browser?: BrowserInfo
  tabs?: unknown[]
  history?: HistoryEntry[]
}

export interface AgentRecord extends AgentSummary {
  session_id?: string
  model?: string
  started_at?: number | string
  result?: string
  error?: string
  browser?: BrowserView
}

export interface Workspace {
  user_data_dir: string
  profile: string
  browser?: BrowserInfo
  tabs?: unknown[]
  busy_agent?: string | null
  idle_seconds?: number
}

export interface DriverInfo {
  key: string
  label: string
  kernel: string
  browser_type: string
  mode: string
  stealth: boolean
  requires?: string | null
  category: 'playwright' | 'system' | 'stealth' | 'remote'
  installable: boolean
  installed: boolean | null
  hint: string
}

export interface DriversResponse {
  playwright_installed: boolean
  drivers: DriverInfo[]
}

// A rendered line in the live stream panel.
export interface Frame {
  event: string
  cls?: string
  text: string
}
