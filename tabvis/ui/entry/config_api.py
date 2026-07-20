"""Runtime configuration surface for the console — read, apply, and persist ``TABVIS_*`` env vars.

tabvis reads its environment **per call** (``get_provider_client`` re-reads ``TABVIS_BASE_URL`` /
``TABVIS_API_KEY`` for every model request; ``browser_config`` re-reads its knobs per launch and per
permission check). So mutating ``os.environ`` takes effect on the *next* run — no restart. This
module exposes that safely:

* **Secrets are write-only.** A value marked ``secret`` is never sent back to the client; the API
  reports only whether it is set, plus a masked hint. There is no endpoint that reveals the key.
* **Writes are loopback-only.** The server has no authentication, so accepting a credential — or a
  ``TABVIS_BASE_URL`` repoint, which would silently redirect every prompt to an attacker — from an
  arbitrary client would be a hole. Config writes are refused unless the request comes from
  localhost, or ``TABVIS_SERVER_ALLOW_REMOTE_CONFIG=1`` is set deliberately.
* **Persistence is a merge, never a clobber.** Values are written to the ``.env`` in the directory
  the server was launched from, preserving any lines we don't manage, with ``0600`` permissions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from tabvis.utils.env_utils import is_env_truthy

LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


@dataclass(frozen=True)
class Setting:
    key: str
    label: str
    group: str
    kind: str  # text | secret | bool | number
    help: str = ""
    placeholder: str = ""
    # For bools: what the code actually does when the var is UNSET, so the console shows what
    # tabvis really does rather than a blank that reads as "off".
    default: str = ""


# The knobs worth exposing — kept in PARITY with .env.example so the console can edit the whole
# configuration surface live (changes apply on the next call/run and are merged into .env).
# Deliberately EXCLUDED (editing these from the console would be a foot-gun): TABVIS_CONFIG_DIR /
# TABVIS_DOTENV / TABVIS_DISABLE_DOTENV (they change where config itself comes from); TABVIS_DEBUG /
# TABVIS_SIMPLE / TABVIS_LOG (presence-checked debug flags that don't fit the form model); and
# TABVIS_SERVER_HOST / TABVIS_SERVER_PORT / TABVIS_SERVER_ALLOW_REMOTE_CONFIG (bind at launch / gate
# config writes themselves). Everything else in .env.example lives below.
SETTINGS: tuple[Setting, ...] = (
    Setting("TABVIS_BASE_URL", "Model endpoint", "Model", "text",
            "Required. No default — tabvis refuses to run without it.",
            "https://api.anthropic.com"),
    Setting("TABVIS_API_KEY", "API key", "Model", "secret",
            "Required (or TABVIS_AUTH_TOKEN). Write-only: never sent back to this page.",
            "sk-ant-…"),
    Setting("TABVIS_AUTH_TOKEN", "Auth token", "Model", "secret",
            "Bearer token — an alternative to the API key. Set one or the other.", ""),
    Setting("TABVIS_MODEL", "Model", "Model", "text",
            "Leave blank for the default. Aliases (tabvis-max…) work here, but not via --model.",
            "claude-sonnet-4-6"),

    Setting("TABVIS_BROWSER_ENGINE", "Engine", "Browser", "text",
            "chromium (stock Playwright) or cloak (CloakBrowser's stealth Chromium — needs "
            "`uv sync --extra cloak`). Each engine keeps its own profile, so switching starts "
            "from a fresh, logged-out browser.",
            "chromium", default="chromium"),
    Setting("TABVIS_BROWSER_HEADLESS", "Headless", "Browser", "bool",
            "Off (the default) gives the agent a REAL browser window you can watch and take over. "
            "Turn on only for CI/containers with no display.",
            default="0"),
    Setting("TABVIS_BROWSER_EAGER", "Pre-launch browser", "Browser", "bool",
            "Warm Chromium at session start. Turn off for non-browsing workloads.",
            default="1"),
    Setting("TABVIS_BROWSER_ALLOWED_DOMAINS", "Allowed domains", "Browser", "text",
            "Comma-separated. EMPTY = allow every domain. Gates BrowserNavigate goto only.",
            "example.com,*.mycompany.com"),
    Setting("TABVIS_BROWSER_TIMEOUT_MS", "Timeout (ms)", "Browser", "number",
            "Per browser operation.", "30000"),

    Setting("TABVIS_WORKSPACE_DIR", "Download workspace", "Browser", "text",
            "Where browser downloads and fetched web PDFs are saved so the agent can Read them. "
            "Blank = per-session default (<config-home>/projects/<cwd>/<session-id>/workspace/). "
            "Set an absolute path to use one fixed folder for every run.",
            "~/tabvis-downloads"),

    # Request pacing — keep the agent a polite client so a rapid navigate/click loop can't burst or
    # DoS a server. Only navigations and clicks to a REAL remote host are paced; localhost and
    # host-less URLs (data:, about:blank) are exempt.
    Setting("TABVIS_BROWSER_MIN_REQUEST_INTERVAL_MS", "Min request interval (ms)", "Browser", "number",
            "Minimum gap between navigations/clicks to the SAME host. 0 disables per-host pacing.",
            "1000", default="1000"),
    Setting("TABVIS_BROWSER_MAX_REQUESTS_PER_MINUTE", "Max requests/min per host", "Browser", "number",
            "Hard per-host burst ceiling (a token bucket over a 60s window). 0 = no ceiling.",
            "0", default="0"),
    Setting("TABVIS_BROWSER_MIN_ACTION_INTERVAL_MS", "Min action interval (ms)", "Browser", "number",
            "Minimum gap between ANY two browser actions (navigate/click/type), across all agents. "
            "0 disables. Use this to stop machine-gun clicking regardless of host.",
            "0", default="0"),
    Setting("TABVIS_BROWSER_REQUEST_JITTER_MS", "Request jitter (ms)", "Browser", "number",
            "Random 0..N ms added to each paced slot so concurrent agents don't fire in lockstep.",
            "0", default="0"),

    # Stealth — read only when the engine is 'cloak'. The license key is a `secret`: like the API
    # key it is write-only, so the console can set it but no endpoint ever reads it back.
    Setting("TABVIS_BROWSER_CLOAK_LICENSE_KEY", "CloakBrowser Pro key", "Stealth", "secret",
            "Optional. Unset = the free-tier binary. Write-only: never sent back to this page.",
            "cb_…"),
    Setting("TABVIS_BROWSER_PROXY", "Proxy", "Stealth", "secret",
            "Routes the browser through a proxy. Treated as a secret — the URL usually carries a "
            "password. cloak engine only.",
            "http://user:pass@host:8080"),
    Setting("TABVIS_BROWSER_HUMANIZE", "Humanize", "Stealth", "bool",
            "Human-like mouse curves and keystroke timing. Beats behavioural detectors, but makes "
            "every click and keypress slower. cloak engine only.",
            default="0"),
    Setting("TABVIS_BROWSER_GEOIP", "Match locale to proxy", "Stealth", "bool",
            "Derive timezone/locale from the proxy's exit IP, so a browser routed through Berlin "
            "does not report a New York clock. cloak engine only.",
            default="0"),
    Setting("TABVIS_BROWSER_HUMAN_PRESET", "Humanize preset", "Stealth", "text",
            "default, or careful (slower and more deliberate, for the strictest behavioural "
            "detectors). Only used when Humanize is on. cloak engine only.",
            "default", default="default"),
    Setting("TABVIS_BROWSER_TIMEZONE", "Timezone", "Stealth", "text",
            "IANA timezone override, e.g. America/New_York. Blank = the host's. cloak engine only.",
            "America/New_York"),
    Setting("TABVIS_BROWSER_LOCALE", "Locale", "Stealth", "text",
            "Locale override, e.g. en-US. Blank = the host's. cloak engine only.",
            "en-US"),

    Setting("TABVIS_SERVER_MAX_AGENTS", "Max agents", "Server", "number",
            "Each running agent is one real Chromium process.", "4"),

    # Vision / OCR — how images reach the model. Multimodal models get native image input; a
    # text-only model can't, so tabvis OCRs images to text when an OCR engine is available.
    Setting("TABVIS_MODEL_SUPPORTS_VISION", "Force vision support", "Vision / OCR", "text",
            "Blank = auto-detect from the model id. Set 1 to force native image input (a custom "
            "multimodal endpoint whose id can't reveal it), or 0 to force the OCR/text path.",
            "1"),
    Setting("TABVIS_OCR_ENABLED", "OCR fallback", "Vision / OCR", "bool",
            "For NON-vision models, extract text from images with Tesseract and send that instead. "
            "Off = images are dropped with a short note. Needs an OCR engine: `uv sync --extra ocr`, "
            "or a tesseract binary on PATH.",
            default="1"),
    Setting("TABVIS_OCR_LANG", "OCR language(s)", "Vision / OCR", "text",
            "Tesseract language code(s), e.g. eng or eng+chi_sim. Extra languages need their "
            "traineddata installed (macOS `brew install tesseract-lang`, Ubuntu "
            "`apt install tesseract-ocr-chi-sim`). Unavailable languages fall back to an installed one.",
            "eng", default="eng"),
    Setting("TABVIS_OCR_ENGINE", "OCR engine", "Vision / OCR", "text",
            "auto (default) | tesserocr | pytesseract | binary — force one OCR engine instead of "
            "auto-detecting.", "auto", default="auto"),

    # =========================================================================================
    # .env.example parity — the rest of the documented configuration surface, editable live.
    # =========================================================================================

    # --- Model (provider, tier repointing, non-Anthropic endpoints) ---
    Setting("TABVIS_MODEL_PROVIDER", "Model provider", "Model", "text",
            "anthropic (default) | openai | gemini. Blank = inferred from the model id.", "openai"),
    Setting("TABVIS_DEFAULT_SONNET_MODEL", "Sonnet-tier model", "Model", "text",
            "Repoint the Balanced/Sonnet tier to your endpoint's model id.", ""),
    Setting("TABVIS_DEFAULT_OPUS_MODEL", "Opus-tier model", "Model", "text",
            "Repoint the Max/Opus tier to your endpoint's model id.", ""),
    Setting("TABVIS_DEFAULT_HAIKU_MODEL", "Haiku-tier model", "Model", "text",
            "Repoint the Fast/Haiku tier to your endpoint's model id.", ""),
    Setting("TABVIS_MAX_OUTPUT_TOKENS", "Max output tokens", "Model", "number",
            "max_tokens per request. Blank = 8192.", "8192"),
    Setting("TABVIS_OPENAI_API_KEY", "OpenAI API key", "Model", "secret",
            "OpenAI-compatible provider key (or OPENAI_API_KEY). Write-only: never sent back.", "sk-…"),
    Setting("TABVIS_OPENAI_BASE_URL", "OpenAI base URL", "Model", "text",
            "OpenAI-compatible endpoint (vLLM / Groq / a gateway).", "https://api.openai.com/v1"),
    Setting("TABVIS_GEMINI_API_KEY", "Gemini API key", "Model", "secret",
            "Gemini provider key (or GEMINI_API_KEY / GOOGLE_API_KEY). Write-only: never sent back.", ""),
    Setting("TABVIS_GEMINI_BASE_URL", "Gemini base URL", "Model", "text",
            "Gemini endpoint override.", ""),
    Setting("TABVIS_CUSTOM_HEADERS", "Custom headers", "Model", "text",
            "Extra request headers, one 'Name: Value' per line (gateways/proxies).", ""),

    # --- Browser (launch/profile knobs; apply on the next run) ---
    Setting("TABVIS_BROWSER_VIEWPORT", "Viewport", "Browser", "text",
            "WIDTHxHEIGHT (lowercase x).", "1280x720"),
    Setting("TABVIS_BROWSER_CHANNEL", "Channel", "Browser", "text",
            "chromium (bundled) | chrome | msedge. Launch-mode Chromium only.", "chromium"),
    Setting("TABVIS_BROWSER_EXECUTABLE_PATH", "Executable path", "Browser", "text",
            "Explicit browser binary — skips `playwright install` / auto-detect.", "/path/to/chrome"),
    Setting("TABVIS_BROWSER_USER_DATA_DIR", "Profile dir", "Browser", "text",
            "Chromium profile (cookies/logins). Blank = the per-engine default under the config home.",
            "~/.tabvis/browser"),
    Setting("TABVIS_BROWSER_AUTO_VISUAL", "Auto visual", "Browser", "bool",
            "Attach a screenshot + trimmed HTML when the accessibility tree is too sparse to reason "
            "from. Off = text-only snapshots.", default="1"),
    Setting("TABVIS_BROWSER_IDLE_TIMEOUT_MS", "Idle reap (ms)", "Browser", "number",
            "Reap an UNOWNED browser workspace after this idle time. 0 = never.", "1800000"),
    Setting("TABVIS_BROWSER_ARGS", "Extra Chromium args", "Browser", "text",
            "Comma-separated, e.g. --no-sandbox.", "--no-sandbox"),
    Setting("TABVIS_BROWSER_SNAPSHOT_MODE", "Snapshot mode", "Browser", "text",
            "'data' forces the injected-attribute snapshot path (else the public aria tree).", "data"),
    Setting("TABVIS_BROWSER_CDP_ENDPOINT", "CDP endpoint", "Browser", "text",
            "DevTools address for cdp-mode engines.", "http://127.0.0.1:9222"),
    Setting("TABVIS_BROWSER_WS_ENDPOINT", "WS endpoint", "Browser", "text",
            "Playwright-server ws:// URL for connect-mode engines (may carry ?token=; redacted in logs).",
            "wss://chrome.browserless.io?token=…"),
    Setting("TABVIS_BROWSER_MAX_PACING_WAIT_MS", "Max pacing wait (ms)", "Browser", "number",
            "Safety cap on a single request-pacing wait.", "60000"),

    # --- Browsing artifacts (the durable trail under the per-session dir) ---
    Setting("TABVIS_BROWSER_ARTIFACTS", "Record artifacts", "Artifacts", "bool",
            "Record the browsing trail (navigation, page metadata, interactions, DOM).", default="1"),
    Setting("TABVIS_BROWSER_ARTIFACTS_DOM", "Capture DOM", "Artifacts", "bool",
            "Capture the page DOM with each artifact event.", default="1"),
    Setting("TABVIS_BROWSER_ARTIFACTS_MAX_DOM_BYTES", "Max DOM bytes", "Artifacts", "number",
            "Cap per captured DOM blob.", "1000000"),
    Setting("TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT", "Redact typed input", "Artifacts", "bool",
            "Store typed text as length-only (hides passwords).", default="0"),

    # --- Stealth (cloak engine) ---
    Setting("TABVIS_BROWSER_CLOAK_VERSION", "CloakBrowser version", "Stealth", "text",
            "Pin a CloakBrowser build (default: whatever ships). cloak engine only.", ""),

    # --- Project instructions ---
    Setting("TABVIS_DISABLE_TABVIS_MDS", "Ignore TABVIS.md", "Project", "bool",
            "Ignore all TABVIS.md project-instruction files.", default="0"),
)

BY_KEY = {s.key: s for s in SETTINGS}


def env_path() -> str:
    """The .env we manage — in the directory the server was launched from (what dotenv loads)."""
    return os.path.join(os.getcwd(), ".env")


def writes_allowed(client_host: str | None) -> bool:
    """Config writes are loopback-only unless explicitly opted out of.

    The server is unauthenticated. Accepting a credential — or a TABVIS_BASE_URL repoint, which would
    silently funnel every prompt to someone else — from a remote client would be a real hole.
    """
    if is_env_truthy(os.environ.get("TABVIS_SERVER_ALLOW_REMOTE_CONFIG")):
        return True
    return (client_host or "") in LOOPBACK


def _mask(value: str) -> str:
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


def read_config(*, reveal_hint: bool = True) -> list[dict[str, Any]]:
    """Current values. Secrets report only *whether* they're set — never the value.

    ``reveal_hint=False`` also withholds the masked hint. Even ``sk-a…9f2c`` exposes the last four
    characters of a live credential, which a remote unauthenticated client has no business seeing;
    only loopback callers get it.
    """
    out: list[dict[str, Any]] = []
    for s in SETTINGS:
        raw = os.environ.get(s.key)
        item: dict[str, Any] = {
            "key": s.key,
            "label": s.label,
            "group": s.group,
            "kind": s.kind,
            "help": s.help,
            "placeholder": s.placeholder,
            "set": bool(raw),
        }
        if s.kind == "secret":
            item["hint"] = _mask(raw) if (raw and reveal_hint) else ""   # never the real value
        else:
            # Fall back to the *effective* default so the console shows what tabvis actually does,
            # not a blank that reads as "off".
            item["value"] = raw if raw is not None else s.default
        out.append(item)
    return out


def _normalize(setting: Setting, value: Any) -> str | None:
    """Form value -> env string. Returns None to mean 'unset this'."""
    if value is None:
        return None
    if setting.kind == "bool":
        if value in ("", None):
            return None
        return "1" if value in (True, "true", "1", "on", "yes") else "0"
    text = str(value).strip()
    if text == "":
        return None
    if setting.kind == "number":
        int(text)  # raises ValueError -> caller turns it into a 400
    return text


def apply_config(values: dict[str, Any]) -> dict[str, Any]:
    """Validate, apply to ``os.environ`` (live — effective on the next run), and persist to .env.

    A secret submitted as an empty string is treated as "leave unchanged", so the console can render
    a blank password box without wiping a key that's already set. Send ``null`` to actually clear it.
    """
    unknown = [k for k in values if k not in BY_KEY]
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")

    applied: list[str] = []
    cleared: list[str] = []
    for key, raw in values.items():
        setting = BY_KEY[key]
        # Blank secret == "don't touch" (the UI never round-trips the real value).
        if setting.kind == "secret" and isinstance(raw, str) and raw.strip() == "":
            continue
        try:
            normalized = _normalize(setting, raw)
        except ValueError as e:
            raise ValueError(f"{key}: expected a number") from e

        if normalized is None:
            if os.environ.pop(key, None) is not None:
                cleared.append(key)
            # The SDK adapter mirrors TABVIS_* onto ANTHROPIC_* — drop the mirror too, or a stale
            # value would linger and quietly win.
            os.environ.pop(f"ANTHROPIC_{key[len('TABVIS_'):]}", None)
        else:
            os.environ[key] = normalized
            applied.append(key)

    _persist({k: os.environ.get(k) for k in values if k in BY_KEY})
    return {"applied": applied, "cleared": cleared, "env_file": env_path()}


def _persist(managed: dict[str, str | None]) -> None:
    """Merge into .env, preserving every line we don't manage. 0600, atomic replace."""
    path = env_path()
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()

        # A commented-out placeholder (`#TABVIS_MODEL=` in .env.example) is the slot this value is
        # *documented* to live in. Adopt it, so a saved value lands in its proper section instead
        # of being dumped at the bottom of the file next to an identical-looking comment.
        if stripped.startswith("#") and "=" in stripped:
            candidate = stripped.lstrip("#").strip().split("=", 1)[0].strip()
            if (
                candidate in managed
                and candidate not in seen
                and managed[candidate] is not None
            ):
                seen.add(candidate)
                out.append(f"{candidate}={managed[candidate]}")
                continue

        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in managed:
            out.append(line)          # not ours — leave it exactly as-is
            continue
        seen.add(key)
        value = managed[key]
        if value is not None:
            out.append(f"{key}={value}")
        # value is None -> drop the line (the setting was cleared)

    for key, value in managed.items():
        if value is not None and key not in seen:
            out.append(f"{key}={value}")

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out).strip() + "\n")
    os.chmod(tmp, 0o600)              # it can hold a credential
    os.replace(tmp, path)
