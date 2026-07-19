"""Browser-agent configuration accessors (env > settings.json > default).

Dedicated env-tunables module for the Playwright browser agent, mirroring the shape of
``tabvis.utils.plan_mode_v2``: one typed accessor per key, each implementing the standard tabvis
precedence — an explicit ``TABVIS_BROWSER_*`` environment variable wins, otherwise the matching
``SettingsJson`` field (session-cached via ``get_initial_settings``), otherwise a hardcoded
default. Never scatter ``os.environ.get`` for browser config across the tool/service code; read
it here.

Engines
-------
``TABVIS_BROWSER_ENGINE`` picks which browser the agent drives. Every engine ends up as an ordinary
Playwright ``BrowserContext``, so *everything downstream is unchanged*: the same Browser* tools, the
same ref/snapshot machinery, the same persistent-profile model. The full matrix lives in
:data:`BROWSER_ENGINE_CATALOG`; the mechanisms are:

- **native launch** — ``chromium`` (default), ``firefox``, ``webkit`` (the three Playwright
  drivers), plus ``chrome``/``msedge`` (installed Chromium via a Playwright *channel*) and
  ``brave``/``vivaldi``/``opera`` (installed Chromium located by binary auto-detection).
- **stealth plugins** — ``cloak`` (CloakBrowser, stealth Chromium; needs ``cloakbrowser``) and
  ``camoufox`` (stealth Firefox; needs ``camoufox``). Each wraps Playwright and returns a context.
- **CDP attach** (``cdp``) — connect to any Chromium exposing a DevTools endpoint
  (``TABVIS_BROWSER_CDP_ENDPOINT``). This is the common denominator for the commercial anti-detect
  browsers (``adspower``/``gologin``/``multilogin``/``octo``/``dolphin``/``kameleo`` — start the
  profile via their local API, pass tabvis the address it returns) and for ``steel``.
- **Playwright-server attach** (``connect``) — connect to a remote Playwright server
  (``TABVIS_BROWSER_WS_ENDPOINT``): ``browserbase``, ``browserless``, a Playwright Docker image.

Environment variables
---------------------
- ``TABVIS_BROWSER_ENGINE``         — any key of :data:`BROWSER_ENGINE_CATALOG` (default ``chromium``).
- ``TABVIS_BROWSER_HEADLESS``       — bool (default False; the browser is the agent's environment).
- ``TABVIS_BROWSER_USER_DATA_DIR``  — path to the persistent profile (default ``<config-home>/browser``,
  or ``<config-home>/browser-<engine>`` for a non-default launch/plugin engine — see
  :func:`get_browser_user_data_dir` for why they must not be shared). Unused by cdp/connect engines.
- ``TABVIS_BROWSER_VIEWPORT``       — ``"WIDTHxHEIGHT"`` (default ``1280x720``).
- ``TABVIS_BROWSER_CHANNEL``        — ``chromium`` | ``chrome`` | ``msedge`` (default ``chromium``).
  launch-mode Chromium engines only.
- ``TABVIS_BROWSER_EXECUTABLE_PATH``— explicit browser binary path (default None => channel/auto-detect).
  launch mode only: plugin/cdp/connect engines drive their own or a remote binary.
- ``TABVIS_BROWSER_CDP_ENDPOINT``   — CDP address for ``cdp``-mode engines (``http://host:port`` or a
  ``ws://…`` browser URL).
- ``TABVIS_BROWSER_WS_ENDPOINT``    — Playwright-server ws endpoint for ``connect``-mode engines
  (may carry a ``?token=`` credential; redacted before it is logged or served).
- ``TABVIS_BROWSER_AUTO_VISUAL``    — bool (default True). When the accessibility snapshot is too
  sparse to reason from (a canvas/visual page), also return a screenshot + trimmed HTML.
- ``TABVIS_BROWSER_TIMEOUT_MS``     — default per-operation timeout in ms (default 30000).
- ``TABVIS_BROWSER_ALLOWED_DOMAINS``— comma-separated host allowlist. **Empty => allow all**
  (navigation to any host is permitted); non-empty => restrict navigation to matching hosts
  (each entry may be an exact host or a ``*.example.com`` wildcard).
- ``TABVIS_BROWSER_ARGS``           — comma-separated extra Chromium launch args.
- ``TABVIS_BROWSER_PROXY``          — proxy URL, e.g. ``http://user:pass@host:8080``. cloak only.
- ``TABVIS_BROWSER_HUMANIZE``       — bool: human-like mouse curves / keystroke timing. cloak only.
- ``TABVIS_BROWSER_HUMAN_PRESET``   — ``default`` | ``careful``. cloak only.
- ``TABVIS_BROWSER_GEOIP``          — bool: derive timezone/locale from the proxy's exit IP. cloak only.
- ``TABVIS_BROWSER_TIMEZONE``       — IANA tz, e.g. ``America/New_York``. cloak only.
- ``TABVIS_BROWSER_LOCALE``         — e.g. ``en-US``. cloak only.
- ``TABVIS_BROWSER_CLOAK_LICENSE_KEY`` — CloakBrowser Pro key (falls back to CloakBrowser's own
  ``CLOAKBROWSER_LICENSE_KEY``). Without one you get the free-tier binary. **Env-only: a secret
  never belongs in settings.json.**
- ``TABVIS_BROWSER_CLOAK_VERSION``  — pin a CloakBrowser Chromium build (default: whatever the
  installed ``cloakbrowser`` ships).

Launch params (engine/headless/user_data_dir/viewport/channel/executable_path/timeout/args, plus
every cloak knob) are read ONCE at browser launch and frozen on the service (settings are
session-cached; freezing keeps a live browser's config stable). The domain allowlist is read per
permission check so runtime grants take effect.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from tabvis.utils.config_constants import (
    BROWSER_CHANNELS,
    BROWSER_ENGINES,
    BROWSER_HUMAN_PRESETS,
)
from tabvis.utils.env_utils import (
    get_tabvis_config_home_dir,
    is_env_defined_falsy,
    is_env_truthy,
)
from tabvis.utils.settings.settings import get_initial_settings

DEFAULT_BROWSER_TIMEOUT_MS = 30_000
# How long a persistent browser workspace may sit idle (nobody driving it) before it is closed.
# 0 = never close it; it lives until the process exits.
DEFAULT_BROWSER_IDLE_TIMEOUT_MS = 30 * 60 * 1000  # 30 minutes
DEFAULT_VIEWPORT: tuple[int, int] = (1280, 720)
DEFAULT_CHANNEL = "chromium"
DEFAULT_ENGINE = "chromium"
DEFAULT_HUMAN_PRESET = "default"

_PLAYWRIGHT_AVAILABLE: bool | None = None
_CLOAKBROWSER_AVAILABLE: bool | None = None
_CAMOUFOX_AVAILABLE: bool | None = None


def playwright_available() -> bool:
    """Whether the ``playwright`` package is importable.

    Lives here (not in ``tabvis.agent.tools.browser_common``) so the browser *service* can gate its
    warm-up on it without importing ``tabvis.agent.tools`` — that package's ``__init__`` imports the
    browser tools, which import the service, which would cycle.
    """
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        _PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None
    return _PLAYWRIGHT_AVAILABLE


def cloakbrowser_available() -> bool:
    """Whether the optional ``cloakbrowser`` package is importable (``uv sync --extra cloak``).

    Note this gates the *engine*, not the browser tools: cloakbrowser hands back a normal Playwright
    context, so ``playwright`` is still required either way.
    """
    global _CLOAKBROWSER_AVAILABLE
    if _CLOAKBROWSER_AVAILABLE is None:
        _CLOAKBROWSER_AVAILABLE = importlib.util.find_spec("cloakbrowser") is not None
    return _CLOAKBROWSER_AVAILABLE


def camoufox_available() -> bool:
    """Whether the optional ``camoufox`` package is importable (``uv sync --extra camoufox``).

    Camoufox is the Firefox-kernel counterpart of CloakBrowser: a stealth-patched Firefox wrapped in
    a Python package that exposes Playwright's own launcher. Like cloak it hands back a normal
    Playwright context, so it gates the *engine* and not the tools — ``playwright`` is required
    either way. Memoized in a module global, so the test fixture resets it between cases.
    """
    global _CAMOUFOX_AVAILABLE
    if _CAMOUFOX_AVAILABLE is None:
        _CAMOUFOX_AVAILABLE = importlib.util.find_spec("camoufox") is not None
    return _CAMOUFOX_AVAILABLE


def engine_package_available(package: str | None) -> bool:
    """Whether an engine's optional backing package is importable (None => nothing required)."""
    if package is None:
        return True
    if package == "cloakbrowser":
        return cloakbrowser_available()
    if package == "camoufox":
        return camoufox_available()
    return importlib.util.find_spec(package) is not None


@dataclass(frozen=True)
class BrowserEngineSpec:
    """How one user-facing engine name resolves to a concrete Playwright launch/connection.

    This is the single source of truth for the browser-compatibility matrix. ``key`` is what the
    user sets in ``TABVIS_BROWSER_ENGINE``; everything else tells the launcher what to do with it. The
    catalog keys are kept byte-identical to ``config_constants.BROWSER_ENGINES`` (a test enforces it).
    """

    key: str
    label: str
    kernel: str           # informational: 'chromium' | 'firefox' | 'webkit'
    browser_type: str     # the Playwright DRIVER: 'chromium' | 'firefox' | 'webkit'
    mode: str             # 'launch' | 'cdp' | 'connect' | 'plugin'
    channel: str | None = None      # launch mode only: Playwright channel ('chrome'/'msedge')
    executable: str | None = None   # launch mode only: engine name for the binary auto-detector
    plugin: str | None = None       # plugin mode only: 'cloak' | 'camoufox'
    stealth: bool = False
    requires: str | None = None     # optional package that must be installed for this engine
    profile_suffix: str = ""        # local persistent-profile dir suffix (launch/plugin only)
    supported: bool = True          # False => launcher refuses with a pointer to the right path
    notes: str = ""

    @property
    def remote(self) -> bool:
        """True when the browser is driven over the wire (no local profile dir of ours)."""
        return self.mode in ("cdp", "connect")


def _spec(
    key: str,
    label: str,
    *,
    kernel: str = "chromium",
    browser_type: str = "chromium",
    mode: str = "launch",
    **kw: object,
) -> BrowserEngineSpec:
    # A ``profile_suffix`` defaults to ``-<key>`` so every launch/plugin engine gets its own profile
    # dir (different binaries and kernels must not share one — see get_browser_user_data_dir); the
    # base ``chromium`` engine keeps the historical bare ``browser`` dir by passing "".
    kw.setdefault("profile_suffix", f"-{key}")
    return BrowserEngineSpec(
        key=key, label=label, kernel=kernel, browser_type=browser_type, mode=mode, **kw  # type: ignore[arg-type]
    )


# The compatibility matrix, keyed by TABVIS_BROWSER_ENGINE value. Order mirrors config_constants.
BROWSER_ENGINE_CATALOG: dict[str, BrowserEngineSpec] = {
    "chromium": _spec(
        "chromium", "Playwright Chromium", profile_suffix="",
        notes="Stock Playwright Chromium. Fast, always available, native automation.",
    ),
    "chrome": _spec(
        "chrome", "Google Chrome", channel="chrome",
        notes="A locally-installed Google Chrome, driven via Playwright channel=chrome.",
    ),
    "msedge": _spec(
        "msedge", "Microsoft Edge", channel="msedge",
        notes="A locally-installed Microsoft Edge, driven via Playwright channel=msedge.",
    ),
    "brave": _spec(
        "brave", "Brave", executable="brave",
        notes="Brave (privacy Chromium). Binary auto-detected; override with "
        "TABVIS_BROWSER_EXECUTABLE_PATH, or attach over CDP with engine=cdp.",
    ),
    "vivaldi": _spec(
        "vivaldi", "Vivaldi", executable="vivaldi",
        notes="Vivaldi. Binary auto-detected; override with TABVIS_BROWSER_EXECUTABLE_PATH.",
    ),
    "opera": _spec(
        "opera", "Opera", executable="opera",
        notes="Opera. Binary auto-detected; override with TABVIS_BROWSER_EXECUTABLE_PATH.",
    ),
    "firefox": _spec(
        "firefox", "Playwright Firefox", kernel="firefox", browser_type="firefox",
        notes="Playwright's automation Firefox. A system Firefox works too via "
        "TABVIS_BROWSER_EXECUTABLE_PATH (launched by Playwright, not reconnected over CDP).",
    ),
    "webkit": _spec(
        "webkit", "Playwright WebKit", kernel="webkit", browser_type="webkit",
        notes="Playwright WebKit — the supported stand-in for Safari (Safari itself cannot be "
        "driven as a Playwright browser).",
    ),
    "cloak": _spec(
        "cloak", "CloakBrowser", mode="plugin", plugin="cloak", stealth=True,
        requires="cloakbrowser", profile_suffix="-cloak",
        notes="Stealth Chromium, fingerprint patched at the C++ source level. "
        "Needs `uv sync --extra cloak`.",
    ),
    "camoufox": _spec(
        "camoufox", "Camoufox", kernel="firefox", browser_type="firefox", mode="plugin",
        plugin="camoufox", stealth=True, requires="camoufox",
        notes="Stealth Firefox. Needs `uv sync --extra camoufox`.",
    ),
    "cdp": _spec(
        "cdp", "CDP endpoint", mode="cdp",
        notes="Attach to any Chromium exposing a CDP endpoint. Set TABVIS_BROWSER_CDP_ENDPOINT "
        "(e.g. http://127.0.0.1:9222 or a ws://…/devtools/browser/… URL).",
    ),
    "connect": _spec(
        "connect", "Playwright server", mode="connect",
        notes="Attach to a remote Playwright server. Set TABVIS_BROWSER_WS_ENDPOINT (ws://…).",
    ),
    "steel": _spec(
        "steel", "Steel Browser", mode="cdp",
        notes="Steel Browser API. Point TABVIS_BROWSER_CDP_ENDPOINT at the session's CDP address "
        "(or TABVIS_BROWSER_WS_ENDPOINT + engine=connect for its Playwright endpoint).",
    ),
    "browserless": _spec(
        "browserless", "Browserless", mode="connect",
        notes="Browserless cloud/self-hosted browsers. Set TABVIS_BROWSER_WS_ENDPOINT to the "
        "wss://…?token=… URL (or engine=cdp with its CDP endpoint).",
    ),
    "browserbase": _spec(
        "browserbase", "Browserbase", mode="connect",
        notes="Browserbase managed sessions. Create a session, then set TABVIS_BROWSER_WS_ENDPOINT "
        "to its connectUrl.",
    ),
    "adspower": _spec(
        "adspower", "AdsPower SunBrowser", mode="cdp",
        notes="Anti-detect. Start the profile via AdsPower's Local API, then set "
        "TABVIS_BROWSER_CDP_ENDPOINT to the ws/puppeteer address it returns.",
    ),
    "gologin": _spec(
        "gologin", "GoLogin Orbita", mode="cdp",
        notes="Anti-detect. Start the profile via GoLogin's API, then set TABVIS_BROWSER_CDP_ENDPOINT "
        "to the returned debugging address.",
    ),
    "multilogin": _spec(
        "multilogin", "Multilogin Mimic", mode="cdp",
        notes="Anti-detect (Mimic = Chromium). Start the profile via Multilogin's Local API, then "
        "set TABVIS_BROWSER_CDP_ENDPOINT. Note: Stealthfox (Firefox) is not driveable this way.",
    ),
    "octo": _spec(
        "octo", "Octo Browser", mode="cdp",
        notes="Anti-detect. Start the profile via Octo's Local API, then set "
        "TABVIS_BROWSER_CDP_ENDPOINT to the returned CDP address.",
    ),
    "dolphin": _spec(
        "dolphin", "Dolphin Anty", mode="cdp",
        notes="Anti-detect. Start the profile via Dolphin Anty's Local API, then set "
        "TABVIS_BROWSER_CDP_ENDPOINT to the returned CDP address.",
    ),
    "kameleo": _spec(
        "kameleo", "Kameleo", mode="cdp",
        notes="Anti-detect. Start the Chromium profile via Kameleo's API, then set "
        "TABVIS_BROWSER_CDP_ENDPOINT to its CDP address.",
    ),
}


def get_engine_spec(engine: str | None = None) -> BrowserEngineSpec:
    """The spec for an engine name (defaults to the configured one), never raising.

    An unknown name resolves to the ``chromium`` spec — same forgiving contract as
    :func:`get_browser_engine`, which has already validated the configured value anyway.
    """
    key = engine or get_browser_engine()
    return BROWSER_ENGINE_CATALOG.get(key, BROWSER_ENGINE_CATALOG[DEFAULT_ENGINE])


def get_browser_engine() -> str:
    """Which browser binary to drive; validated against BROWSER_ENGINES, else 'chromium'.

    An unknown value falls back to the default rather than raising — same contract as
    :func:`get_browser_channel`. Note this reports what the user *asked for*; whether the optional
    ``cloakbrowser`` package is actually installed is :func:`cloakbrowser_available`, and reconciling
    the two is the launcher's job (it refuses, loudly, rather than silently downgrading a stealth
    request to a browser that will get the agent blocked).
    """
    raw = (
        os.environ.get("TABVIS_BROWSER_ENGINE")
        or get_initial_settings().browser_engine
        or DEFAULT_ENGINE
    )
    return raw if raw in BROWSER_ENGINES else DEFAULT_ENGINE


def is_browser_stealth() -> bool:
    """Whether the configured engine is a stealth build (CloakBrowser / Camoufox)."""
    return get_engine_spec().stealth


def get_browser_type() -> str:
    """Which Playwright DRIVER the configured engine uses: chromium | firefox | webkit."""
    return get_engine_spec().browser_type


def get_browser_connect_mode() -> str:
    """How the configured engine obtains its context: launch | cdp | connect | plugin."""
    return get_engine_spec().mode


def is_browser_auto_visual() -> bool:
    """Whether to auto-attach a screenshot + trimmed HTML when the aria snapshot is too sparse.

    Default **on**: a canvas game, a maps/whiteboard app or an image-only page produces an
    accessibility tree with almost nothing to reason from, so the observe loop supplements it with a
    screenshot and the page HTML. Set ``TABVIS_BROWSER_AUTO_VISUAL=0`` to force the text-only snapshot
    (e.g. a token-sensitive run that never wants image blocks). An *explicit* screenshot request
    (``BrowserSnapshot(include_screenshot=true)``) is unaffected — it is always honoured.
    """
    env = os.environ.get("TABVIS_BROWSER_AUTO_VISUAL")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    return True


DEFAULT_ARTIFACTS_MAX_DOM_BYTES = 1_000_000


def is_browser_artifacts_enabled() -> bool:
    """Whether to record the agent's browsing trail (navigation / page / interaction / DOM).

    Default **on** — the artifacts store is the audit/replay record of what the agent did in the
    browser. ``TABVIS_BROWSER_ARTIFACTS=0`` turns it off (no events written, no DOM captured).
    """
    env = os.environ.get("TABVIS_BROWSER_ARTIFACTS")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    return True


def is_browser_artifacts_dom_enabled() -> bool:
    """Whether each artifact captures the page DOM (HTML). Default on; off saves an evaluate + disk."""
    env = os.environ.get("TABVIS_BROWSER_ARTIFACTS_DOM")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    return True


def get_browser_artifacts_max_dom_bytes() -> int:
    """Cap on a single captured DOM blob (bytes). Larger pages are truncated. Default 1 MB."""
    raw = os.environ.get("TABVIS_BROWSER_ARTIFACTS_MAX_DOM_BYTES")
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return DEFAULT_ARTIFACTS_MAX_DOM_BYTES


def is_browser_artifacts_redact_input() -> bool:
    """Redact typed text in interaction artifacts (default off).

    The agent's keystrokes can include credentials typed into a login form. On => the artifact
    stores only the length, not the text.
    """
    return is_env_truthy(os.environ.get("TABVIS_BROWSER_ARTIFACTS_REDACT_INPUT"))


def is_browser_eager_disabled() -> bool:
    """``TABVIS_BROWSER_EAGER=0`` opts out of the launch-at-session-start warm-up."""
    return is_env_defined_falsy(os.environ.get("TABVIS_BROWSER_EAGER"))


def is_browser_headless() -> bool:
    """Run the browser headless. **Default False** — the browser is the agent's environment.

    The agent lives in a real, visible browser window: you can watch it navigate, see what it saw,
    and take the wheel yourself. Set TABVIS_BROWSER_HEADLESS=1 for CI/containers — or just let it be,
    since a headed launch with no display degrades to headless automatically (see BrowserService).
    """
    env = os.environ.get("TABVIS_BROWSER_HEADLESS")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    val = get_initial_settings().browser_headless
    if val is not None:
        return bool(val)
    return False


def is_browser_headless_explicit() -> bool:
    """Whether the user actually asked for a headless mode (vs. just taking the default).

    If they did, a failed launch must NOT be silently retried in the other mode — that would be us
    overriding an explicit instruction.
    """
    env = os.environ.get("TABVIS_BROWSER_HEADLESS")
    if is_env_truthy(env) or is_env_defined_falsy(env):
        return True
    return get_initial_settings().browser_headless is not None


def get_browser_user_data_dir() -> str:
    """Persistent Chromium profile dir (cookies/logins live here).

    **Each engine gets its own profile by default** (``browser`` vs ``browser-cloak``). They must not
    be shared: a Chromium profile is migrated forward on open and is *not* safe to hand back to an
    older build, and the two engines are different Chromium versions (CloakBrowser pins its own
    patched build, which trails Playwright's bundled one). Pointing the older binary at a profile the
    newer one has already upgraded is how you get a refused launch or a corrupted profile. Keeping
    them apart also means a stealth session never inherits cookies, storage and fingerprint-adjacent
    state written by a browser that was never trying to be stealthy.

    An explicit ``TABVIS_BROWSER_USER_DATA_DIR`` (or the settings field) still wins — pointing both
    engines at one directory is then the user's deliberate call, and the manager will refuse to hand
    a live workspace to the wrong engine rather than let them collide.
    """
    explicit = (
        os.environ.get("TABVIS_BROWSER_USER_DATA_DIR")
        or get_initial_settings().browser_user_data_dir
    )
    if explicit:
        return explicit
    # Each engine gets its OWN profile dir by default (``browser`` for stock chromium, ``browser-
    # cloak`` for cloak, ``browser-firefox`` for firefox, …). A profile is migrated forward on open
    # and is not safe to hand back to a different build or kernel, so the defaults must not collide.
    # Remote engines (cdp/connect) never launch a local profile of ours, but a stable per-engine key
    # is still handy for the workspace registry, so they keep a suffix too (it just isn't written).
    name = "browser" + get_engine_spec().profile_suffix
    return os.path.join(get_tabvis_config_home_dir(), name)


def get_browser_channel() -> str:
    """Browser channel; validated against BROWSER_CHANNELS, else default 'chromium'.

    Precedence: explicit env/setting > the engine's intrinsic channel (``chrome`` for the ``chrome``
    engine, ``msedge`` for ``msedge``) > the bundled default. Only consulted by launch-mode Chromium
    engines — Firefox/WebKit have no channel, and connect/cdp/plugin engines ignore it entirely.
    """
    raw = (
        os.environ.get("TABVIS_BROWSER_CHANNEL")
        or get_initial_settings().browser_channel
        or get_engine_spec().channel
        or DEFAULT_CHANNEL
    )
    return raw if raw in BROWSER_CHANNELS else DEFAULT_CHANNEL


# Common install locations for the Chromium-family browsers Playwright has no ``channel`` for
# (Brave/Vivaldi/Opera). Chrome and Edge are reached through their channels instead, so they are
# absent here. Keyed by the spec's ``executable`` detector name; each maps to per-platform
# candidates tried in order.
_BROWSER_BINARY_CANDIDATES: dict[str, dict[str, tuple[str, ...]]] = {
    "brave": {
        "darwin": ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",),
        "linux": ("brave-browser", "brave", "brave-browser-stable"),
        "win32": (
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        ),
    },
    "vivaldi": {
        "darwin": ("/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",),
        "linux": ("vivaldi", "vivaldi-stable"),
        "win32": (r"C:\Program Files\Vivaldi\Application\vivaldi.exe",),
    },
    "opera": {
        "darwin": ("/Applications/Opera.app/Contents/MacOS/Opera",),
        "linux": ("opera",),
        "win32": (
            r"C:\Program Files\Opera\opera.exe",
            r"C:\Program Files (x86)\Opera\opera.exe",
        ),
    },
}


def detect_browser_binary(name: str | None) -> str | None:
    """Best-effort absolute path to a well-known Chromium-family binary, or None if not found.

    Used for the engines Playwright cannot select by channel (Brave/Vivaldi/Opera). Each candidate is
    either an absolute path (checked with ``os.path.exists``) or a bare command resolved on ``PATH``
    via ``shutil.which``. Deliberately conservative and side-effect-free — a miss just returns None,
    and the launcher then reports a clear "install it or set TABVIS_BROWSER_EXECUTABLE_PATH" error
    rather than Playwright's opaque one.
    """
    if not name:
        return None
    candidates = _BROWSER_BINARY_CANDIDATES.get(name, {}).get(sys.platform)
    if not candidates:
        return None
    for cand in candidates:
        if os.path.isabs(cand):
            if os.path.exists(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def get_browser_executable_path() -> str | None:
    """Explicit browser binary path, else an auto-detected one for Brave/Vivaldi/Opera, else None.

    An explicit ``TABVIS_BROWSER_EXECUTABLE_PATH`` (or the setting) always wins. Otherwise, if the
    engine is one Playwright has no channel for, we try to locate its binary in the usual places; a
    miss returns None (Playwright then falls back to its bundled Chromium, or the launcher errors).
    """
    explicit = (
        os.environ.get("TABVIS_BROWSER_EXECUTABLE_PATH")
        or get_initial_settings().browser_executable_path
    )
    if explicit:
        return explicit
    return detect_browser_binary(get_engine_spec().executable)


def get_browser_cdp_endpoint() -> str | None:
    """CDP endpoint for ``cdp``-mode engines (``http://host:port`` or a ``ws://…`` browser URL).

    This is what a Chromium started with ``--remote-debugging-port``, Steel, Browserless-over-CDP,
    and every commercial anti-detect browser (whose local API returns such an address) are attached
    through. Env-first, then settings; None when unset (the launcher then errors with guidance).
    """
    return (
        os.environ.get("TABVIS_BROWSER_CDP_ENDPOINT")
        or get_initial_settings().browser_cdp_endpoint
        or None
    )


def get_browser_ws_endpoint() -> str | None:
    """Playwright-server ws endpoint for ``connect``-mode engines (Browserbase/Browserless/Docker).

    ``ws://`` or ``wss://`` (Browserless URLs carry a ``?token=…``, so this can hold a credential —
    it is redacted in browser_info() via :func:`redact_proxy`). Env-first, then settings.
    """
    return (
        os.environ.get("TABVIS_BROWSER_WS_ENDPOINT")
        or get_initial_settings().browser_ws_endpoint
        or None
    )


def get_browser_timeout_ms() -> int:
    """Default per-operation timeout in ms (env parsed + clamped, then settings, then default)."""
    env = os.environ.get("TABVIS_BROWSER_TIMEOUT_MS")
    if env:
        try:
            parsed = int(env)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    val = get_initial_settings().browser_timeout_ms
    if isinstance(val, int) and val > 0:
        return val
    return DEFAULT_BROWSER_TIMEOUT_MS


def get_browser_viewport() -> tuple[int, int]:
    """(width, height); env ``WIDTHxHEIGHT``, else settings ``{width,height}``, else default."""
    raw = os.environ.get("TABVIS_BROWSER_VIEWPORT")
    if raw and "x" in raw:
        w, _, h = raw.partition("x")
        try:
            return (int(w), int(h))
        except ValueError:
            pass
    val = get_initial_settings().browser_viewport
    if isinstance(val, dict) and "width" in val and "height" in val:
        try:
            return (int(val["width"]), int(val["height"]))
        except (ValueError, TypeError):
            pass
    return DEFAULT_VIEWPORT


def get_browser_allowed_domains() -> list[str]:
    """Host allowlist. Empty list => allow ALL domains (the user-chosen default posture)."""
    raw = os.environ.get("TABVIS_BROWSER_ALLOWED_DOMAINS")
    if raw is not None:
        return [d for d in (s.strip() for s in raw.split(",")) if d]
    val = get_initial_settings().browser_allowed_domains
    if isinstance(val, list):
        return [str(d).strip() for d in val if str(d).strip()]
    return []


def get_browser_idle_timeout_ms() -> int:
    """Close a persistent workspace after this long with no agent driving it. 0 = never.

    The browser is the agent's environment and deliberately outlives a run — but one that nobody
    ever returns to is just a leaked Chromium, so idle workspaces are reaped.
    """
    env = os.environ.get("TABVIS_BROWSER_IDLE_TIMEOUT_MS")
    if env:
        try:
            parsed = int(env)
            if parsed >= 0:
                return parsed
        except ValueError:
            pass
    return DEFAULT_BROWSER_IDLE_TIMEOUT_MS


def get_browser_launch_args() -> list[str]:
    """Extra Chromium launch args (comma-separated ``TABVIS_BROWSER_ARGS``), else []."""
    raw = os.environ.get("TABVIS_BROWSER_ARGS")
    if raw:
        return [a for a in (s.strip() for s in raw.split(",")) if a]
    return []


# --------------------------------------------------------------------------- cloak engine
#
# Everything below is read only when the engine is 'cloak'. They are all settings-backed EXCEPT the
# license key, which is a credential and is therefore env-only — tabvis's settings.json is world-
# readable config that gets echoed to the console, and a paid key has no business in it.


def get_browser_proxy() -> str | None:
    """Proxy URL for the browser (``http://user:pass@host:8080``, or a socks5:// URL), else None."""
    return (
        os.environ.get("TABVIS_BROWSER_PROXY")
        or get_initial_settings().browser_proxy
        or None
    )


# Matches the userinfo of any URL that carries a password — ``scheme://user:pass@`` — even embedded
# in a larger string, e.g. a Chromium ``--proxy-server=socks5://user:pass@host`` argv echoed inside a
# launch-failure message. Only the credentials are captured; the scheme and host survive for
# debugging. ``[^\s/@]+`` for the password permits an embedded ``:`` (or percent-encoded byte) and
# stops at the first ``@``.
_URL_CREDENTIAL_RE = re.compile(r"([a-zA-Z][\w+.-]*://)[^\s/:@]+:[^\s/@]+@")


def redact_proxy(url: str | None) -> str | None:
    """A proxy URL with its credentials stripped, safe to log / persist / serve.

    ``browser_info()`` is written to ``browser-session.json`` and served over the (unauthenticated)
    HTTP API, so the raw URL — which routinely carries ``user:pass`` — must never reach it. This
    function's entire job is to be safe to serve, so it never raises: a malformed URL (a bad port, a
    stray character) degrades to a sentinel rather than propagating a ``ValueError`` up into
    ``browser_info()`` and crashing record persistence / the ``/browsers`` endpoint.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        # ``.hostname`` and ``.port`` parse lazily — ``.port`` raises on an out-of-range or
        # non-numeric value, so both belong INSIDE the guard, not after it.
        host, port, user = parts.hostname, parts.port, parts.username
        scheme, path = parts.scheme, parts.path
    except ValueError:
        return "(unparseable proxy url)"
    if not host:
        return "(proxy set)"
    if ":" in host:  # IPv6 literal — urlsplit strips the [] that the URL form requires
        host = f"[{host}]"
    netloc = host + (f":{port}" if port else "")
    if user:
        netloc = f"***@{netloc}"
    return urlunsplit((scheme, netloc, path, "", ""))


def scrub_secrets(text: str) -> str:
    """Redact inline URL credentials (a proxy ``user:pass``) from an arbitrary string.

    A launch failure echoes the full Chromium argv, which under the cloak engine carries the proxy
    URL verbatim. So any error string we PERSIST (``browser-session.json``) or LOG could otherwise
    leak the proxy password even though the structured fields are redacted. Route those strings
    through here to hold the same no-credentials posture everywhere.
    """
    if not text:
        return text
    return _URL_CREDENTIAL_RE.sub(r"\1***@", text)


def is_browser_humanize() -> bool:
    """Human-like mouse curves / keystroke timing / scroll (default False).

    Off by default because it is not free: every click and keystroke grows a randomised delay, so a
    long form-filling run gets materially slower. Turn it on when a site is scoring *behaviour*, not
    just fingerprints.
    """
    env = os.environ.get("TABVIS_BROWSER_HUMANIZE")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    return bool(get_initial_settings().browser_humanize)


def get_browser_human_preset() -> str:
    """``default`` | ``careful``; validated against BROWSER_HUMAN_PRESETS, else ``default``."""
    raw = (
        os.environ.get("TABVIS_BROWSER_HUMAN_PRESET")
        or get_initial_settings().browser_human_preset
        or DEFAULT_HUMAN_PRESET
    )
    return raw if raw in BROWSER_HUMAN_PRESETS else DEFAULT_HUMAN_PRESET


def is_browser_geoip() -> bool:
    """Derive timezone/locale from the proxy's exit IP (default False).

    Only meaningful with a proxy: it is what stops a browser that *claims* to be in Berlin from
    reporting a New York clock. Costs a lookup against CloakBrowser's GeoIP service at launch.
    """
    env = os.environ.get("TABVIS_BROWSER_GEOIP")
    if is_env_truthy(env):
        return True
    if is_env_defined_falsy(env):
        return False
    return bool(get_initial_settings().browser_geoip)


def get_browser_timezone() -> str | None:
    """IANA timezone override (e.g. ``America/New_York``), else None (use the host's)."""
    return (
        os.environ.get("TABVIS_BROWSER_TIMEZONE")
        or get_initial_settings().browser_timezone
        or None
    )


def get_browser_locale() -> str | None:
    """Locale override (e.g. ``en-US``), else None (use the host's)."""
    return (
        os.environ.get("TABVIS_BROWSER_LOCALE")
        or get_initial_settings().browser_locale
        or None
    )


def get_cloak_license_key() -> str | None:
    """CloakBrowser Pro key. **Env-only** — a credential does not go in settings.json.

    ``CLOAKBROWSER_LICENSE_KEY`` is CloakBrowser's own variable; honour it too so a machine already
    set up for CloakBrowser needs no tabvis-specific configuration. Unset => the free-tier binary.
    """
    return (
        os.environ.get("TABVIS_BROWSER_CLOAK_LICENSE_KEY")
        or os.environ.get("CLOAKBROWSER_LICENSE_KEY")
        or None
    )


def get_cloak_browser_version() -> str | None:
    """Pin a specific CloakBrowser Chromium build, else None (whatever cloakbrowser ships)."""
    return (
        os.environ.get("TABVIS_BROWSER_CLOAK_VERSION")
        or get_initial_settings().browser_cloak_version
        or None
    )


@dataclass(frozen=True)
class CloakLaunchConfig:
    """Snapshot of the cloak-engine knobs. Only read when ``engine == 'cloak'``."""

    proxy: str | None = None
    humanize: bool = False
    human_preset: str = DEFAULT_HUMAN_PRESET
    geoip: bool = False
    timezone: str | None = None
    locale: str | None = None
    license_key: str | None = None
    browser_version: str | None = None

    def redacted(self) -> dict[str, object]:
        """A JSON-safe view with the credentials removed — for browser_info()/the session record."""
        return {
            "proxy": redact_proxy(self.proxy),
            "humanize": self.humanize,
            "human_preset": self.human_preset if self.humanize else None,
            "geoip": self.geoip,
            "timezone": self.timezone,
            "locale": self.locale,
            "licensed": bool(self.license_key),  # never the key itself
            "browser_version": self.browser_version,
        }


@dataclass(frozen=True)
class BrowserLaunchConfig:
    """Snapshot of launch-time browser config, frozen on the service at launch."""

    headless: bool
    user_data_dir: str
    viewport: tuple[int, int]
    channel: str
    executable_path: str | None
    timeout_ms: int
    engine: str = DEFAULT_ENGINE
    # How this engine is realised (from the catalog): which driver, how it connects, its kernel and
    # whether it is a stealth build. Frozen here so a live browser's identity stays stable even if
    # settings change under it.
    browser_type: str = DEFAULT_ENGINE          # 'chromium' | 'firefox' | 'webkit'
    mode: str = "launch"                          # 'launch' | 'cdp' | 'connect' | 'plugin'
    kernel: str = DEFAULT_ENGINE                  # informational: 'chromium'|'firefox'|'webkit'
    stealth: bool = False
    # Remote-attach endpoints — only one is meaningful, chosen by ``mode``.
    cdp_endpoint: str | None = None               # mode == 'cdp'
    ws_endpoint: str | None = None                # mode == 'connect'
    launch_args: list[str] = field(default_factory=list)
    cloak: CloakLaunchConfig = field(default_factory=CloakLaunchConfig)


def load_browser_launch_config() -> BrowserLaunchConfig:
    """Read every launch param ONCE (call at browser launch, never per action)."""
    spec = get_engine_spec()
    return BrowserLaunchConfig(
        engine=spec.key,
        browser_type=spec.browser_type,
        mode=spec.mode,
        kernel=spec.kernel,
        stealth=spec.stealth,
        headless=is_browser_headless(),
        user_data_dir=get_browser_user_data_dir(),
        viewport=get_browser_viewport(),
        channel=get_browser_channel(),
        executable_path=get_browser_executable_path(),
        cdp_endpoint=get_browser_cdp_endpoint(),
        ws_endpoint=get_browser_ws_endpoint(),
        timeout_ms=get_browser_timeout_ms(),
        launch_args=get_browser_launch_args(),
        cloak=CloakLaunchConfig(
            proxy=get_browser_proxy(),
            humanize=is_browser_humanize(),
            human_preset=get_browser_human_preset(),
            geoip=is_browser_geoip(),
            timezone=get_browser_timezone(),
            locale=get_browser_locale(),
            license_key=get_cloak_license_key(),
            browser_version=get_cloak_browser_version(),
        ),
    )
