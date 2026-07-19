"""Dependency-free config enum constants

The TS file is intentionally import-free (kept separate to avoid circular-dependency issues)
and exports three ``as const`` string tuples enumerating valid config values: notification
channels, editor modes, and teammate spawn modes.

Casing: these are UPPER_CASE module constants (PEP 8 for module-level constants). The string
*values* inside are config wire values and are kept verbatim from the TS source. The TS tuples
are modelled as Python ``tuple``s (immutable, like ``as const``).
"""

from __future__ import annotations

from typing import Final, Literal

NOTIFICATION_CHANNELS: Final[
    tuple[
        Literal["auto"],
        Literal["iterm2"],
        Literal["iterm2_with_bell"],
        Literal["terminal_bell"],
        Literal["kitty"],
        Literal["ghostty"],
        Literal["notifications_disabled"],
    ]
] = (
    "auto",
    "iterm2",
    "iterm2_with_bell",
    "terminal_bell",
    "kitty",
    "ghostty",
    "notifications_disabled",
)

# Valid editor modes (excludes deprecated 'emacs' which is auto-migrated to 'normal').
EDITOR_MODES: Final[tuple[Literal["normal"], Literal["vim"]]] = ("normal", "vim")

# Valid teammate modes for spawning:
#   'tmux'       = traditional tmux-based teammates
#   'in-process' = in-process teammates running in same process
#   'auto'       = automatically choose based on context (default)
TEAMMATE_MODES: Final[
    tuple[Literal["auto"], Literal["tmux"], Literal["in-process"]]
] = ("auto", "tmux", "in-process")

# Valid browser channels for the browser-agent persistent context (Playwright ``channel=``):
#   'chromium' = the Playwright-bundled Chromium build (default; passed as *no* channel)
#   'chrome'   = a locally-installed Google Chrome
#   'msedge'   = a locally-installed Microsoft Edge
# Only consulted by ``launch``-mode Chromium engines — a stealth build (cloak) ships its own patched
# binary and a connect/cdp engine attaches to a browser someone else launched, so both ignore it.
BROWSER_CHANNELS: Final[
    tuple[Literal["chromium"], Literal["chrome"], Literal["msedge"]]
] = (
    "chromium",
    "chrome",
    "msedge",
)

# The three Playwright DRIVERS. Every engine ultimately drives one of these — a chromium-kernel
# browser (Chrome/Edge/Brave/Vivaldi/Opera/anti-detect) is driven by the ``chromium`` driver, a
# Firefox-kernel one (incl. Camoufox) by ``firefox``, a WebKit/Safari-like one by ``webkit``.
BROWSER_TYPES: Final[
    tuple[Literal["chromium"], Literal["firefox"], Literal["webkit"]]
] = (
    "chromium",
    "firefox",
    "webkit",
)

# HOW a Playwright BrowserContext is obtained for an engine. All four end at the same place — an
# ordinary ``BrowserContext`` — so the Browser* tools, snapshot/ref machinery and observe→act loop
# are identical regardless of mode:
#   'launch'   = Playwright launches the binary itself (``*.launch_persistent_context``), the
#                persistent-profile path the agent was built around.
#   'cdp'      = attach to a browser someone else already launched, over the Chrome DevTools
#                Protocol (``chromium.connect_over_cdp(endpoint)``). This is the common denominator
#                for the commercial anti-detect browsers (their local API hands back a CDP address),
#                for a Chromium started with ``--remote-debugging-port``, and for Steel/Browserless.
#   'connect'  = attach to a remote Playwright *server* over its websocket protocol
#                (``*.connect(ws_endpoint)``) — Browserbase, Browserless, a Playwright Docker image.
#   'plugin'   = a stealth SDK that wraps Playwright and returns a context of its own (CloakBrowser,
#                Camoufox). Which SDK is named by the engine, not this field.
BROWSER_CONNECT_MODES: Final[
    tuple[Literal["launch"], Literal["cdp"], Literal["connect"], Literal["plugin"]]
] = (
    "launch",
    "cdp",
    "connect",
    "plugin",
)

# Which browser the agent drives, by user-facing name. Every engine ends up as a Playwright
# BrowserContext (see BROWSER_CONNECT_MODES), so the Browser* tools and the persistent-profile model
# are engine-agnostic; the engine only decides which binary/driver and how it is obtained. The rich
# per-engine spec (driver, kernel, mode, channel, stealth, profile dir, required package) lives in
# ``tabvis.utils.browser_config.BROWSER_ENGINE_CATALOG`` — this tuple is just the set of valid keys,
# kept here (dependency-free) for the ``get_browser_engine`` membership check and typing. A test
# asserts the two stay in sync.
#
#   chromium   stock Playwright Chromium (default)          |  launch, chromium driver
#   chrome     locally-installed Google Chrome              |  launch, channel=chrome
#   msedge     locally-installed Microsoft Edge             |  launch, channel=msedge
#   brave      Brave (privacy Chromium)                     |  launch, executable auto-detected
#   vivaldi    Vivaldi                                       |  launch, executable auto-detected
#   opera      Opera                                         |  launch, executable auto-detected
#   firefox    Playwright Firefox                            |  launch, firefox driver
#   webkit     Playwright WebKit (stands in for Safari)      |  launch, webkit driver
#   cloak      CloakBrowser — stealth Chromium               |  plugin (needs `cloakbrowser`)
#   camoufox   Camoufox — stealth Firefox                    |  plugin (needs `camoufox`)
#   cdp        attach to any CDP endpoint you supply         |  cdp
#   connect    attach to any Playwright server ws endpoint   |  connect
#   steel      Steel Browser (open/hosted browser API)       |  cdp
#   browserless  Browserless remote browser service          |  connect
#   browserbase  Browserbase managed browser service         |  connect
#   adspower   AdsPower SunBrowser (anti-detect)             |  cdp
#   gologin    GoLogin Orbita (anti-detect)                  |  cdp
#   multilogin Multilogin Mimic (anti-detect)               |  cdp
#   octo       Octo Browser (anti-detect)                    |  cdp
#   dolphin    Dolphin Anty (anti-detect)                    |  cdp
#   kameleo    Kameleo (anti-detect)                         |  cdp
BROWSER_ENGINES: Final[
    tuple[
        Literal["chromium"],
        Literal["chrome"],
        Literal["msedge"],
        Literal["brave"],
        Literal["vivaldi"],
        Literal["opera"],
        Literal["firefox"],
        Literal["webkit"],
        Literal["cloak"],
        Literal["camoufox"],
        Literal["cdp"],
        Literal["connect"],
        Literal["steel"],
        Literal["browserless"],
        Literal["browserbase"],
        Literal["adspower"],
        Literal["gologin"],
        Literal["multilogin"],
        Literal["octo"],
        Literal["dolphin"],
        Literal["kameleo"],
    ]
] = (
    "chromium",
    "chrome",
    "msedge",
    "brave",
    "vivaldi",
    "opera",
    "firefox",
    "webkit",
    "cloak",
    "camoufox",
    "cdp",
    "connect",
    "steel",
    "browserless",
    "browserbase",
    "adspower",
    "gologin",
    "multilogin",
    "octo",
    "dolphin",
    "kameleo",
)

# CloakBrowser's human-behaviour presets, used only when humanize is on:
#   'default' = human-ish mouse curves / keystroke timing
#   'careful' = slower, more deliberate; for the strictest behavioural detectors
BROWSER_HUMAN_PRESETS: Final[tuple[Literal["default"], Literal["careful"]]] = (
    "default",
    "careful",
)
