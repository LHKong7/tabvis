"""BrowserService — owns the Playwright persistent context and drives page actions.

One instance (held by :mod:`tabvis.browser.manager`) owns a single
``launch_persistent_context`` BrowserContext for the whole run: a persistent Chromium profile
whose cookies/logins survive across tool calls and across ``tabvis`` invocations. Every public
action is serialized by an ``asyncio.Lock`` (a shared page is stateful — two CDP actions must
never interleave) and bounded by ``asyncio.wait_for`` (a hung navigation must never freeze the
single agent event loop).

Playwright is imported lazily inside :meth:`launch` so merely importing this module (e.g. when
the tool registry loads) never requires Playwright to be installed.

Element addressing: :meth:`observe` produces a compact snapshot where each interactive/named
node is tagged ``[ref=eN]``. Two mechanisms, tried in order:

1. **aria-ref** — the public ``page.aria_snapshot(mode="ai")`` (Playwright ≳1.50); refs resolve via
   the ``aria-ref=eN`` selector engine, no DOM mutation. ``boxes=True`` adds ``[box=x,y,w,h]`` per
   node so refs line up with a screenshot. The private ``page._snapshot_for_ai()`` is a fallback for
   older Playwright.
2. **data-attr fallback** — a single ``page.evaluate`` walks the DOM, tags actionable nodes
   with ``data-tabvis-ref="eN"``, and refs resolve via ``[data-tabvis-ref='eN']``.

When the accessibility tree is too sparse to reason from (a canvas game, a map, an image-only page),
:meth:`observe` supplements the snapshot with a screenshot and a trimmed copy of the page HTML — see
:func:`_aria_is_thin` and ``TABVIS_BROWSER_AUTO_VISUAL``.

Refs are valid only for the most recent snapshot; acting on a ref that no longer resolves raises
:class:`StaleRefError`, which the tool surfaces as a recoverable "re-snapshot" error.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from tabvis.browser.rate_limiter import get_request_pacer, host_of
from tabvis.browser.session import utc_now
from tabvis.utils.browser_config import (
    BrowserLaunchConfig,
    camoufox_available,
    cloakbrowser_available,
    is_browser_headless_explicit,
    load_browser_launch_config,
    redact_proxy,
    scrub_secrets,
)
from tabvis.utils.debug import log_for_debugging

if TYPE_CHECKING:  # pragma: no cover - typing only; playwright is a lazy runtime import
    from playwright.async_api import BrowserContext, Locator, Page

# How long to let a freshly-navigated page settle (networkidle) before snapshotting it. Bounded:
# a page that never goes idle (polling, ads, websockets) must not hang the agent.
_SETTLE_SECONDS = 6.0

# Extra time to allow a humanized (cloak) action, on top of the normal per-op timeout. Typing is
# paced per character (~0.5s each, measured); a click first walks the cursor to the target.
_HUMANIZE_SECONDS_PER_CHAR = 1.2
_HUMANIZE_CLICK_SECONDS = 15.0

# Keep snapshots comfortably under the 50k tool-result persistence cap (utils/tool_result_storage).
_SNAPSHOT_CHAR_BUDGET = 40_000
# How much of a page's readable text to give the model. The rest of the budget goes to the
# interactive elements, which it needs in order to act.
_TEXT_BUDGET = 22_000
# Budget for the trimmed page HTML attached when the aria snapshot is too sparse to reason from.
# Small on purpose: it is a *reasoning aid* alongside the screenshot, not the primary observation,
# and the sparse-aria case leaves plenty of the result budget free anyway.
_HTML_BUDGET = 12_000

# When is the accessibility snapshot "not enough"? A canvas game / maps / whiteboard / image-only
# page yields an aria tree with almost no named nodes and almost no text — below both thresholds we
# treat it as sparse and supplement it with a screenshot + trimmed HTML (see BrowserService.observe).
_ARIA_THIN_MAX_REFS = 3
_ARIA_THIN_MAX_CHARS = 400

# Native input timings mirror the deterministic sequencing described in the browser-operation
# design.  They are deliberately small and fixed: their purpose is to preserve browser event order,
# not to pretend that deterministic automation has a human biometric signature.
_SCROLL_INTO_VIEW_DELAY = 0.05
_MOUSE_MOVE_DELAY = 0.05
_MOUSE_HOLD_DELAY = 0.08
_SCROLL_STEP_DELAY = 0.15

# One pass to lift a trimmed copy of the page HTML: drop non-content/heavy nodes and defuse inline
# data: URIs (base64 images blow the budget and say nothing). Operates on a clone — no DOM mutation.
_HTML_EXTRACT_JS = r"""
() => {
  const root = document.body || document.documentElement;
  if (!root) return '';
  const clone = root.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,template,svg,link,meta,iframe').forEach(e => e.remove());
  clone.querySelectorAll('[src],[href]').forEach(e => {
    for (const a of ['src', 'href']) {
      const v = e.getAttribute(a);
      if (v && v.startsWith('data:')) e.setAttribute(a, 'data:…');
    }
  });
  return clone.outerHTML || '';
}
"""

# One JS pass: clear old refs, tag visible interactive/named nodes, return a compact node list.
_SNAPSHOT_JS = r"""
() => {
  const INTERACTIVE_TAGS = new Set([
    'A','BUTTON','INPUT','TEXTAREA','SELECT','DETAILS','SUMMARY','OPTION'
  ]);
  const INTERACTIVE_ROLES = new Set(['button','link','textbox','searchbox','checkbox','radio',
    'combobox','menuitem','menuitemcheckbox','menuitemradio','tab','switch','option','slider']);
  document.querySelectorAll('[data-tabvis-ref]').forEach(el => el.removeAttribute('data-tabvis-ref'));
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = window.getComputedStyle(el);
    return !(s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0');
  };
  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  const accName = (el) => {
    let name = el.getAttribute('aria-label') || '';
    const lb = el.getAttribute('aria-labelledby');
    if (!name && lb) {
      name = lb.split(/\s+/).map(id => {
        const e = document.getElementById(id); return e ? e.innerText : '';
      }).join(' ');
    }
    if (!name) name = el.getAttribute('placeholder') || el.getAttribute('title') ||
      el.getAttribute('alt') || '';
    if (!name && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) name = el.value || '';
    if (!name) name = el.innerText || el.textContent || '';
    return clean(name);
  };
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    switch (el.tagName) {
      case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
      case 'BUTTON': return 'button';
      case 'SELECT': return 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'SUMMARY': return 'button';
      case 'INPUT': {
        const it = (el.getAttribute('type') || 'text').toLowerCase();
        if (['button','submit','reset','image'].includes(it)) return 'button';
        if (it === 'checkbox') return 'checkbox';
        if (it === 'radio') return 'radio';
        if (it === 'search') return 'searchbox';
        return 'textbox';
      }
      default: return 'generic';
    }
  };
  const out = [];
  let n = 0;
  const sel = 'a,button,input,textarea,select,details,summary,option,[role],[onclick],' +
    '[onmousedown],[onmouseup],' +
    '[contenteditable=""],[contenteditable="true"],[tabindex]';
  for (const el of document.querySelectorAll(sel)) {
    const role = roleOf(el);
    const interactive = INTERACTIVE_TAGS.has(el.tagName) || INTERACTIVE_ROLES.has(role) ||
      el.hasAttribute('onclick') || el.hasAttribute('onmousedown') ||
      el.hasAttribute('onmouseup') || el.hasAttribute('tabindex') || el.isContentEditable;
    if (!interactive || !isVisible(el) || el.disabled) continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-tabvis-ref', ref);
    const entry = { ref, role, name: accName(el) };
    if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
      entry.value = clean(el.value).slice(0, 60);
    }
    out.push(entry);
  }
  // The page's READABLE TEXT. Without this the agent can see a page's buttons but cannot read a
  // word of it — useless for the main job of a browser agent, which is reading the web.
  const strip = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','IFRAME']);
  const main = document.querySelector('main,article,[role=main]') || document.body;
  let text = '';
  if (main) {
    const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
        const p = node.parentElement;
        if (!p || strip.has(p.tagName)) return NodeFilter.FILTER_REJECT;
        const st = window.getComputedStyle(p);
        if (st.display === 'none' || st.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const parts = [];
    let node;
    while ((node = walker.nextNode())) parts.push(node.nodeValue.trim());
    text = parts.join(' ').replace(/\s+/g, ' ').trim();
  }
  return { url: location.href, title: document.title, nodes: out, text };
}
"""


class BrowserError(Exception):
    """A browser action failed in a way the model can recover from."""


class StaleRefError(BrowserError):
    """A ref no longer resolves against the current page (the page changed)."""


class BrowserService:
    """Drives a single persistent Chromium context. All public actions are lock-serialized."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: BrowserContext | None = None
        # Set for connect/cdp engines: the remote Browser we attached to. Closing it disconnects
        # (it does NOT kill the remote browser). None for engines that launch a persistent context.
        self._connected_browser: Any = None
        # True when the engine's own SDK context-manager (Camoufox) was entered into the exit stack
        # and will tear itself down — so we must not also push a context.close callback.
        self._engine_manages_teardown = False
        self._exit_stack: contextlib.AsyncExitStack | None = None
        self._active_page: Page | None = None
        self._action_lock = asyncio.Lock()
        self._closed = False
        self._config: BrowserLaunchConfig | None = None
        # Snapshot state — which mechanism produced the last snapshot and on which page.
        self._ref_mode = "data"  # "aria" | "data"
        self._snapshot_page: Page | None = None
        self._snapshot_gen = 0
        # Identity, for the session record.
        self.launched_at: str | None = None
        self._driver_pid: int | None = None
        self._browser_version: str | None = None
        self._resolved_executable: str | None = None
        # cloak engine only: the patched Chromium we resolved (and, on first run, downloaded).
        self._cloak_binary: str | None = None
        # Files saved into the download workspace (browser downloads + fetched web PDFs). Surfaced to
        # the agent via observe() so it knows where to Read them. _downloads_reported tracks how many
        # have already been announced so each is reported once.
        self._downloads: list[dict[str, Any]] = []
        self._downloads_reported = 0

    # ------------------------------------------------------------------ lifecycle

    async def launch(self, user_data_dir: str | None = None) -> None:
        """Launch or attach the browser context on the running loop (lazy Playwright import).

        ``user_data_dir`` overrides the configured profile dir. For a *launched* engine Chromium
        takes a **single-writer lock** on a profile, so two concurrently-running agents MUST be
        given different dirs — that is how per-agent browser isolation is achieved (see
        services/agents/registry.py). Remote engines (cdp/connect) have no local profile, so the
        dir is ignored for them.

        Five ways produce the context (``TABVIS_BROWSER_ENGINE`` → the catalog's mode):
        native ``launch_persistent_context`` (chromium/firefox/webkit, incl. chrome/edge/brave/…),
        a CloakBrowser or Camoufox stealth *plugin*, a ``connect_over_cdp`` attach, or a Playwright-
        server ``connect``. The branch ends here — all five yield an ordinary Playwright
        ``BrowserContext``, so every line below, and every Browser* tool, is engine-agnostic.
        """
        cfg = load_browser_launch_config()
        if user_data_dir:
            cfg = replace(cfg, user_data_dir=user_data_dir)
        self._config = cfg
        self._exit_stack = contextlib.AsyncExitStack()

        # IDP-5: per-identity environment/network overlay for this launch. Empty for a fresh identity
        # (all env/network fields None), so the default launch is byte-for-byte unchanged.
        try:
            from tabvis.browser import identity_store
            from tabvis.browser.manager import current_agent_id

            self._identity_overlay = identity_store.launch_overlay(current_agent_id())
        except Exception:  # noqa: BLE001 - the overlay is best-effort
            self._identity_overlay = {}

        self._preflight(cfg)  # engine prerequisites — fail loud, before any half-launched state

        # Only engines we launch ourselves own a local profile dir; a cdp/connect attach does not.
        launches_locally = cfg.mode in ("launch", "plugin")
        if launches_locally:
            os.makedirs(cfg.user_data_dir, exist_ok=True)

        # A headed launch on a machine with no display is doomed, and we can often tell up front.
        # Going straight to headless then is not just tidier — under a plugin engine a *failed*
        # headed attempt strands its Playwright driver, because the SDK starts its own Playwright
        # inside the launch and only stops it via the context it never returns; the retry below
        # starts a second one. So when there is provably no display, skip the doomed attempt rather
        # than launch headed just to catch the failure. Headless is meaningless for a remote attach,
        # so this only applies to engines we launch. Still honour an explicit mode.
        if (
            launches_locally
            and not cfg.headless
            and not is_browser_headless_explicit()
            and not _display_available()
        ):
            log_for_debugging("[BROWSER] no display detected; launching headless.")
            cfg = replace(cfg, headless=True)
            self._config = cfg

        try:
            self._context = await self._open_context(cfg, cfg.headless)
        except Exception as e:
            # A display was advertised (or we could not tell) but the headed launch still failed —
            # CI, a container, a bare SSH session. Degrade to headless rather than failing the run.
            # Only meaningful for engines we launch; if the user ASKED for a mode explicitly, or a
            # remote attach failed, honour it and let the error surface.
            # NOTE: under a plugin engine this retry orphans the failed attempt's Playwright driver
            # (see above) — unavoidable once the attempt is made, which is why the pre-check exists
            # to avoid it in the common no-display case. The driver is reaped at process exit.
            if not launches_locally or cfg.headless or is_browser_headless_explicit():
                raise
            log_for_debugging(
                f"[BROWSER] headed launch failed ({scrub_secrets(str(e))}); "
                f"falling back to headless."
            )
            cfg = replace(cfg, headless=True)
            self._config = cfg
            self._context = await self._open_context(cfg, True)

        # Register teardown. A connect/cdp attach closes its Browser (disconnect). A Camoufox plugin
        # entered its own async context-manager into the stack and tears itself down. Otherwise the
        # persistent context is closed directly — which under cloak also stops cloakbrowser's own
        # Playwright (it patches context.close()), and under a native launch unwinds async_playwright
        # (entered in _launch_native, also on the stack).
        if self._connected_browser is not None:
            self._exit_stack.push_async_callback(self._connected_browser.close)
        elif not self._engine_manages_teardown:
            self._exit_stack.push_async_callback(self._context.close)
        self._context.set_default_timeout(cfg.timeout_ms)
        self._context.set_default_navigation_timeout(cfg.timeout_ms)
        self._context.on("page", self._on_page)
        self._context.on("close", lambda: setattr(self, "_closed", True))
        self._ensure_active_page()
        self.launched_at = utc_now()
        self._driver_pid = _playwright_driver_pid(self._pw, self._context)
        # A persistent context still exposes its Browser (version + resolved binary) in Playwright.
        browser = self._context.browser
        if browser is not None:
            with contextlib.suppress(Exception):
                self._browser_version = browser.version
                self._resolved_executable = browser.browser_type.executable_path
        if cfg.engine == "cloak" and self._cloak_binary:
            # browser_type.executable_path reports Playwright's OWN bundled Chromium, which is not
            # what is running — cloak launched its patched binary via executable_path. Report the
            # binary we actually resolved, or the session record would name the wrong browser.
            self._resolved_executable = self._cloak_binary
        log_for_debugging(
            f"[BROWSER] launched persistent context (engine={cfg.engine}, "
            f"headless={cfg.headless}, profile={cfg.user_data_dir})"
        )

    def _preflight(self, cfg: BrowserLaunchConfig) -> None:
        """Reject a misconfigured engine loudly, before any browser process is spawned.

        Refusing beats silently downgrading: someone who asked for a stealth engine is browsing
        somewhere that will block a stock fingerprint, and someone who selected a remote engine has
        nothing for us to attach to without an endpoint. A clear error naming the fix is far better
        than a browser that only *appears* to work.
        """
        if cfg.engine == "cloak":
            if not cloakbrowser_available():
                raise BrowserError(
                    "TABVIS_BROWSER_ENGINE=cloak, but the 'cloakbrowser' package is not installed. "
                    "Install the optional extra (`uv sync --extra cloak`), or set "
                    "TABVIS_BROWSER_ENGINE=chromium to use stock Playwright Chromium."
                )
            if cfg.channel != "chromium" or cfg.executable_path:
                # CloakBrowser drives its own source-patched binary; a channel/executable pointing
                # at stock Chrome would defeat the entire point, so it is ignored rather than
                # silently honoured. Say so — a silently-dropped setting is a debugging nightmare.
                log_for_debugging(
                    "[BROWSER] engine=cloak ignores TABVIS_BROWSER_CHANNEL / "
                    "TABVIS_BROWSER_EXECUTABLE_PATH — it drives its own patched Chromium."
                )
        elif cfg.engine == "camoufox":
            if not camoufox_available():
                raise BrowserError(
                    "TABVIS_BROWSER_ENGINE=camoufox, but the 'camoufox' package is not installed. "
                    "Install the optional extra (`uv sync --extra camoufox`), or set "
                    "TABVIS_BROWSER_ENGINE=firefox to use stock Playwright Firefox."
                )
        elif cfg.mode == "cdp" and not cfg.cdp_endpoint:
            raise BrowserError(
                f"TABVIS_BROWSER_ENGINE={cfg.engine} attaches over CDP, but no endpoint is set. "
                "Set TABVIS_BROWSER_CDP_ENDPOINT to the browser's DevTools address "
                "(e.g. http://127.0.0.1:9222, or the ws://…/devtools/browser/… URL its API returns)."
            )
        elif cfg.mode == "connect" and not cfg.ws_endpoint:
            raise BrowserError(
                f"TABVIS_BROWSER_ENGINE={cfg.engine} attaches to a Playwright server, but no endpoint "
                "is set. Set TABVIS_BROWSER_WS_ENDPOINT to the ws:// / wss:// connect URL."
            )

    async def _open_context(self, cfg: BrowserLaunchConfig, headless: bool) -> Any:
        """Dispatch on the engine's mode, returning a Playwright ``BrowserContext`` either way."""
        if cfg.mode == "cdp":
            return await self._connect_cdp(cfg)
        if cfg.mode == "connect":
            return await self._connect_ws(cfg)
        if cfg.engine == "cloak":
            return await self._launch_cloak(headless)
        if cfg.engine == "camoufox":
            return await self._launch_camoufox(headless)
        return await self._launch_native(headless)

    async def _launch_native(self, headless: bool) -> Any:
        """Native ``launch_persistent_context`` on the chromium/firefox/webkit driver.

        Covers stock Chromium, Chrome/Edge (via ``channel``), Brave/Vivaldi/Opera and any custom
        Chromium (via ``executable_path``), Playwright Firefox/WebKit, and a system Firefox pointed
        at by ``executable_path``. Owns its Playwright via the service's exit stack.
        """
        from playwright.async_api import async_playwright

        cfg = self._config
        assert cfg is not None and self._exit_stack is not None
        if self._pw is None:
            self._pw = await self._exit_stack.enter_async_context(async_playwright())

        driver = getattr(self._pw, cfg.browser_type)  # chromium | firefox | webkit
        kwargs: dict[str, Any] = {
            "user_data_dir": cfg.user_data_dir,
            "headless": headless,
            "executable_path": cfg.executable_path or None,
            "viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]},
            "args": cfg.launch_args or None,
            "accept_downloads": True,  # captured to the workspace via _on_download
        }
        # A ``channel`` is a Chromium-only concept, and channel="chromium" is the sentinel for "the
        # bundled build", selected by passing NO channel. Firefox/WebKit have no channels at all.
        if cfg.browser_type == "chromium" and cfg.channel and cfg.channel != "chromium":
            kwargs["channel"] = cfg.channel
        # IDP-5: overlay per-identity environment/network (only fields the identity actually set).
        overlay = getattr(self, "_identity_overlay", None) or {}
        for key in ("locale", "timezone_id", "user_agent"):
            if overlay.get(key):
                kwargs[key] = overlay[key]
        if overlay.get("viewport"):
            kwargs["viewport"] = overlay["viewport"]
        if overlay.get("proxy"):
            kwargs["proxy"] = {"server": overlay["proxy"]}
        return await driver.launch_persistent_context(**kwargs)

    async def _connect_cdp(self, cfg: BrowserLaunchConfig) -> Any:
        """Attach to a running Chromium over the DevTools Protocol (``connect_over_cdp``).

        The engine kernel is always chromium here (CDP is a Chromium protocol) — this is how the
        commercial anti-detect browsers, a ``--remote-debugging-port`` Chrome, Steel and
        Browserless-over-CDP are driven. We attach to a browser we did not start, so teardown only
        *disconnects*; the remote browser keeps running (its own manager owns its lifecycle).
        """
        from playwright.async_api import async_playwright

        assert self._exit_stack is not None
        if self._pw is None:
            self._pw = await self._exit_stack.enter_async_context(async_playwright())
        browser = await self._pw.chromium.connect_over_cdp(
            cfg.cdp_endpoint, timeout=cfg.timeout_ms
        )
        self._connected_browser = browser
        return await self._context_from(browser)

    async def _connect_ws(self, cfg: BrowserLaunchConfig) -> Any:
        """Attach to a remote Playwright *server* over its websocket (``<driver>.connect``).

        Browserbase, Browserless and a Playwright Docker image expose a Playwright endpoint rather
        than raw CDP. As with cdp, teardown disconnects rather than killing the remote browser.
        """
        from playwright.async_api import async_playwright

        assert self._exit_stack is not None
        if self._pw is None:
            self._pw = await self._exit_stack.enter_async_context(async_playwright())
        driver = getattr(self._pw, cfg.browser_type)
        browser = await driver.connect(cfg.ws_endpoint, timeout=cfg.timeout_ms)
        self._connected_browser = browser
        return await self._context_from(browser)

    async def _context_from(self, browser: Any) -> Any:
        """Reuse a remote browser's existing context, or open one — the shared 'attach' tail.

        A CDP attach to a real browser already has a default context (its open windows); a fresh
        Playwright-server connection may have none. Reusing the existing one keeps the agent in the
        browser the user actually set up (its tabs, its logins).
        """
        cfg = self._config
        assert cfg is not None
        contexts = list(getattr(browser, "contexts", []) or [])
        if contexts:
            return contexts[0]
        return await browser.new_context(
            viewport={"width": cfg.viewport[0], "height": cfg.viewport[1]}
        )

    async def _launch_camoufox(self, headless: bool) -> Any:
        """Camoufox — a stealth-patched Firefox wrapped as a Playwright launcher.

        The Firefox-kernel counterpart of the cloak engine. ``camoufox`` exposes ``AsyncCamoufox``,
        an async context-manager that starts its own Playwright and yields a browser/context; we
        enter it into the service's exit stack so its teardown runs on close (hence
        ``_engine_manages_teardown`` — we must NOT also push a context.close). With
        ``persistent_context=True`` it writes the same on-disk profile model as every other engine.
        """
        from camoufox.async_api import AsyncCamoufox  # type: ignore[import-untyped]

        cfg = self._config
        assert cfg is not None and self._exit_stack is not None
        cloak = cfg.cloak  # proxy / geoip / locale / timezone are shared stealth knobs
        # IDP-5: per-identity proxy / locale override the camoufox stealth knobs when set.
        overlay = getattr(self, "_identity_overlay", None) or {}
        _proxy = overlay.get("proxy") or cloak.proxy
        camoufox = AsyncCamoufox(
            headless=headless,
            persistent_context=True,
            user_data_dir=cfg.user_data_dir,
            proxy={"server": _proxy} if _proxy else None,
            geoip=cloak.geoip,
            locale=overlay.get("locale") or cloak.locale or None,
            args=cfg.launch_args or None,
        )
        obj = await self._exit_stack.enter_async_context(camoufox)
        self._engine_manages_teardown = True
        # persistent_context=True yields a BrowserContext; a plain Browser (no persistence) is
        # coerced to one. Either way the exit stack owns teardown.
        if hasattr(obj, "new_context"):
            return await self._context_from(obj)
        return obj

    async def _launch_cloak(self, headless: bool) -> Any:
        """CloakBrowser's stealth Chromium — a fingerprint patched at the C++ source level.

        Returns a plain Playwright ``BrowserContext``, so nothing downstream changes. Two details
        that are not obvious from cloakbrowser's API:

        * It starts its **own** Playwright and monkey-patches ``context.close()`` to stop it. We must
          therefore NOT enter ``async_playwright()`` ourselves for this engine (that would leave a
          second, orphaned driver process), and ``self._pw`` stays None.
        * ``ensure_binary()`` downloads ~140MB of Chromium on first use, and cloakbrowser calls it
          **synchronously from inside** the async launcher. On tabvis's single agent event loop that
          would block *everything* — the model stream, other tools, the server — for the length of
          the download. So we resolve it in a thread first; the call inside the launcher then hits
          a warm cache and returns instantly.
        """
        from cloakbrowser import ensure_binary, launch_persistent_context_async

        cfg = self._config
        assert cfg is not None
        cloak = cfg.cloak

        if self._cloak_binary is None:
            self._cloak_binary = await asyncio.to_thread(
                ensure_binary,
                license_key=cloak.license_key,
                browser_version=cloak.browser_version,
            )
            log_for_debugging(f"[BROWSER] cloak binary ready: {self._cloak_binary}")

        # IDP-5: per-identity environment/network overrides the cloak stealth knobs when set.
        overlay = getattr(self, "_identity_overlay", None) or {}
        return await launch_persistent_context_async(
            user_data_dir=cfg.user_data_dir,
            headless=headless,
            viewport=overlay.get("viewport") or {"width": cfg.viewport[0], "height": cfg.viewport[1]},
            args=cfg.launch_args or None,
            proxy=overlay.get("proxy") or cloak.proxy or None,
            geoip=cloak.geoip,
            humanize=cloak.humanize,
            human_preset=cloak.human_preset,
            timezone=overlay.get("timezone_id") or cloak.timezone or None,
            locale=overlay.get("locale") or cloak.locale or None,
            license_key=cloak.license_key or None,
            browser_version=cloak.browser_version or None,
        )

    def browser_info(self) -> dict[str, Any]:
        """JSON-safe description of the running browser, for the session record.

        This is persisted to ``browser-session.json`` and served over the HTTP API, so it must carry
        no credentials — the cloak block is deliberately the *redacted* view (proxy password
        stripped, license key reduced to a boolean).
        """
        cfg = self._config
        engine = cfg.engine if cfg else "chromium"
        mode = cfg.mode if cfg else "launch"
        remote = mode in ("cdp", "connect")
        info: dict[str, Any] = {
            "engine": engine,
            "browser_type": cfg.browser_type if cfg else "chromium",
            "kernel": cfg.kernel if cfg else "chromium",
            "mode": mode,
            "stealth": cfg.stealth if cfg else False,
            "version": self._browser_version,
            # A remote attach has no local persistent context of ours — it rides the remote browser's.
            "persistent_context": not remote,
            "profile_dir": None if remote else (cfg.user_data_dir if cfg else None),
            "headless": None if remote else (cfg.headless if cfg else None),
            "channel": cfg.channel if cfg else None,
            "executable_path": self._resolved_executable
            or (cfg.executable_path if cfg else None),
            # A cdp/connect endpoint may carry a token; redact before it is persisted / served.
            "cdp_endpoint": redact_proxy(cfg.cdp_endpoint) if cfg else None,
            "ws_endpoint": redact_proxy(cfg.ws_endpoint) if cfg else None,
            "viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]} if cfg else None,
            "launch_args": list(cfg.launch_args) if cfg else [],
            "timeout_ms": cfg.timeout_ms if cfg else None,
            "launched_at": self.launched_at,
            "driver_pid": self._driver_pid,
            "ref_mode": self._ref_mode,
        }
        if cfg and cfg.stealth:
            info["cloak"] = cfg.cloak.redacted()
        return info

    def tabs(self) -> list[dict[str, Any]]:
        """JSON-safe snapshot of the open tabs (``page.url`` is a sync property)."""
        out: list[dict[str, Any]] = []
        active = self._active_page
        for i, page in enumerate(self._pages()):
            if page.is_closed():
                continue
            out.append({"index": i, "url": page.url, "active": page is active})
        return out

    async def close(self) -> None:
        """Best-effort teardown; close the context and stop Playwright exactly once."""
        self._closed = True
        stack = self._exit_stack
        self._exit_stack = None
        if stack is not None:
            with contextlib.suppress(BaseException):
                await stack.aclose()
        self._context = None
        self._connected_browser = None
        self._active_page = None
        self._snapshot_page = None

    def is_alive(self) -> bool:
        return self._context is not None and not self._closed

    # ------------------------------------------------------------------ page/tab tracking

    def _on_page(self, page: Page) -> None:
        # A popup / target=_blank / window.open — make it the active page and de-register on close.
        self._active_page = page
        page.on("close", lambda: self._on_page_close(page))
        # Capture every browser download into the workspace (see downloads.py).
        page.on("download", self._on_download)
        # OBS-6: attach observation producers (download / console). No-op unless the bus is on;
        # fully best-effort so it can never affect page tracking.
        try:
            from tabvis.browser.manager import current_agent_id
            from tabvis.browser.observation_adapters import attach_page_producers

            attach_page_producers(page, agent_id=current_agent_id())
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ downloads / workspace

    def _record_download(
        self,
        path: str,
        url: str | None,
        kind: str,
        *,
        action: str,
        policy_effect: str | None = None,
        policy_rule_id: str | None = None,
        quarantined: bool = False,
        expose: bool = True,
    ) -> dict[str, Any]:
        """Record a saved file: surface it to the agent (unless quarantined) and log a download
        artifact (issue #5) so the fetch is auditable alongside navigation/click/policy events."""
        entry: dict[str, Any] = {
            "path": path,
            "url": url,
            "filename": os.path.basename(path),
            "kind": kind,
        }
        if quarantined:
            entry["quarantined"] = True
        # A quarantined file is deliberately NOT added to _downloads: observe() reports that list to
        # the agent, and a file policy did not clear must not be handed to the model.
        if expose and not quarantined:
            self._downloads.append(entry)
        where = "quarantine" if quarantined else "workspace"
        log_for_debugging(f"[BROWSER] saved {kind} to {where}: {path}")
        self._schedule_download_artifact(
            action=action,
            url=url,
            path=path,
            filename=entry["filename"],
            policy_effect=policy_effect,
            policy_rule_id=policy_rule_id,
            quarantined=quarantined,
        )
        return entry

    def _schedule_download_artifact(self, **kwargs: Any) -> None:
        """Fire-and-forget the ``type=download`` artifact write. Best-effort; never blocks a save."""
        try:
            from tabvis.browser.artifacts import record_download_artifact

            task = asyncio.ensure_future(record_download_artifact(**kwargs))
            self._download_tasks = getattr(self, "_download_tasks", set())
            self._download_tasks.add(task)
            task.add_done_callback(self._download_tasks.discard)
        except Exception as e:  # noqa: BLE001
            log_for_debugging(f"[BROWSER] failed to schedule download artifact: {e}")

    @staticmethod
    def _judge_download(url: str | None) -> tuple[str, str | None]:
        """Evaluate ``browser.download`` for an *unexpected* download. Fail closed on any error."""
        try:
            from tabvis.browser.manager import current_agent_id
            from tabvis.browser.policy_guard import evaluate_download

            return evaluate_download(url or "", current_agent_id())
        except Exception as e:  # noqa: BLE001 - a policy hiccup must not silently expose a file
            log_for_debugging(f"[BROWSER] download policy evaluation failed, quarantining: {e}")
            return "deny", None

    def _on_download(self, download: Any) -> None:
        """Playwright download event — save the file off the event loop, policy permitting."""
        task = asyncio.ensure_future(self._save_download(download))
        # Keep a reference so the task isn't GC'd mid-flight; drop it when done.
        self._download_tasks = getattr(self, "_download_tasks", set())
        self._download_tasks.add(task)
        task.add_done_callback(self._download_tasks.discard)

    async def _save_download(self, download: Any) -> None:
        """Handle an *unexpected* browser download (a click that triggered one).

        Issue #3: such a download is not the same as an explicit BrowserDownload — the click being
        allowed does NOT imply the resulting file is. So it is pre-judged against ``browser.download``:
        an ``allow`` lands it in the workspace (agent-visible); anything else is held in quarantine,
        recorded as an artifact, and kept out of the agent's reach for a human to review.
        """
        from tabvis.browser.downloads import get_quarantine_dir, get_workspace_dir, unique_path

        url = getattr(download, "url", None)
        suggested = getattr(download, "suggested_filename", None)
        try:
            effect, rule_id = self._judge_download(url)
            if effect == "allow":
                dest = unique_path(get_workspace_dir(), suggested)
                await download.save_as(dest)
                self._record_download(
                    dest, url, "download", action="click_download",
                    policy_effect=effect, policy_rule_id=rule_id,
                )
            else:
                dest = unique_path(get_quarantine_dir(), suggested)
                await download.save_as(dest)
                self._record_download(
                    dest, url, "download", action="click_download",
                    policy_effect=effect, policy_rule_id=rule_id,
                    quarantined=True, expose=False,
                )
                log_for_debugging(
                    f"[BROWSER] unexpected download quarantined (policy={effect}): {url}"
                )
        except Exception as e:  # noqa: BLE001 - a failed download must never break the run
            log_for_debugging(f"[BROWSER] download save failed: {e}")

    async def _capture_pdf_navigation(self, response: Any, url: str) -> None:
        """If a navigation landed on a PDF, save its bytes to the workspace (Chromium would only
        render it in the built-in viewer, which the accessibility snapshot can't read).

        The navigation itself was already policy-checked (``browser.navigate``), so the PDF the agent
        deliberately navigated to is workspace-visible; it is still logged as a ``pdf_navigation``
        download artifact for the audit trail."""
        if response is None:
            return
        try:
            ctype = (response.headers or {}).get("content-type", "") if hasattr(response, "headers") else ""
            is_pdf = "application/pdf" in ctype.lower() or urlparse(url).path.lower().endswith(".pdf")
            if not is_pdf:
                return
            body = await response.body()
            from tabvis.browser.downloads import filename_from_url, get_workspace_dir, unique_path

            dest = unique_path(get_workspace_dir(), filename_from_url(url, "page.pdf"))
            with open(dest, "wb") as fh:
                fh.write(body)
            self._record_download(dest, url, "pdf", action="pdf_navigation", policy_effect="allow")
        except Exception as e:  # noqa: BLE001 - best-effort; the page still rendered
            log_for_debugging(f"[BROWSER] pdf capture failed: {e}")

    async def clear_origin_data(self, origin: str) -> dict[str, Any]:
        """Clear one origin's cookies + storage on the live context (issue #4). Browser must be up."""
        from tabvis.browser.data_clearing import clear_origin_data

        async with self._action_lock:
            if self._context is None:
                raise BrowserError("Browser is not running.")
            return await clear_origin_data(self._context, origin)

    async def download(self, url: str, *, filename: str | None = None) -> dict[str, Any]:
        """Fetch ``url`` through the browser context (so cookies/auth apply) into the workspace.

        This is the *explicit* BrowserDownload path — the tool already cleared ``browser.download``
        via ``check_permissions`` before we get here, so the file is agent-visible; it is recorded as
        an ``explicit_download`` artifact."""
        from tabvis.browser.downloads import filename_from_url, get_workspace_dir, unique_path

        async with self._action_lock:
            if self._context is None:
                raise BrowserError("Browser is not running.")
            await get_request_pacer().pace(host_of(url), counts_as_request=True)
            resp = await self._context.request.get(url)
            if not resp.ok:
                raise BrowserError(f"Download failed: HTTP {resp.status} for {url}")
            body = await resp.body()
            dest = unique_path(get_workspace_dir(), filename or filename_from_url(url))
            with open(dest, "wb") as fh:
                fh.write(body)
            entry = self._record_download(
                dest, url, "download", action="explicit_download", policy_effect="allow"
            )
            return {"downloaded": entry, "workspace": get_workspace_dir()}

    def _on_page_close(self, page: Page) -> None:
        if self._active_page is page:
            pages = [p for p in self._pages() if not p.is_closed()]
            self._active_page = pages[-1] if pages else None

    def _pages(self) -> list[Page]:
        return list(self._context.pages) if self._context is not None else []

    def _ensure_active_page(self) -> Page:
        if self._context is None:
            raise BrowserError("Browser is not running.")
        if self._active_page is not None and not self._active_page.is_closed():
            return self._active_page
        open_pages = [p for p in self._pages() if not p.is_closed()]
        self._active_page = open_pages[-1] if open_pages else None
        return self._active_page  # type: ignore[return-value]

    @property
    def active_page(self) -> Page:
        page = self._ensure_active_page()
        if page is None:
            raise BrowserError("No open browser page.")
        return page

    def _timeout_s(self) -> float:
        ms = self._config.timeout_ms if self._config else 30_000
        # Bound the whole op slightly above Playwright's own timeout so a wedged call still unwinds.
        return ms / 1000 + 5

    def _humanizing(self) -> bool:
        """Whether CloakBrowser is currently pacing our clicks and keystrokes like a human's."""
        cfg = self._config
        return bool(cfg and cfg.engine == "cloak" and cfg.cloak.humanize)

    def _action_timeout_s(self, text_len: int = 0) -> float:
        """Bound for one *user-input* action (click / type), as opposed to a page-level op.

        Humanized input is deliberately slow: CloakBrowser puts a randomised pause between every
        keystroke and moves the cursor along a Bézier curve before it clicks. Measured, that runs at
        roughly half a second per character — so the flat ``timeout_ms + 5`` that is generous for a
        plain ``locator.fill()`` will cancel a humanized fill of anything longer than a short phrase,
        killing it mid-word. The budget therefore has to grow with the text being typed. The
        per-character allowance is ~2x the measured cost, because the pauses are randomised and the
        occasional one is long; this is a ceiling for a wedged call, not a target.
        """
        budget = self._timeout_s()
        if self._humanizing():
            budget += _HUMANIZE_CLICK_SECONDS + text_len * _HUMANIZE_SECONDS_PER_CHAR
        return budget

    # ------------------------------------------------------------------ observation

    async def observe(self, *, include_screenshot: bool = False) -> dict[str, Any]:
        """Snapshot the active page. The core 'return state' payload.

        The primary observation is the accessibility snapshot (refs the agent acts on). When that
        snapshot is too sparse to reason from — a canvas game, a maps/whiteboard app, an image-only
        page — the aria tree describes almost nothing, so we **supplement** it with a screenshot and
        a trimmed copy of the page HTML (unless ``TABVIS_BROWSER_AUTO_VISUAL=0``). An explicit
        ``include_screenshot`` is always honoured and also asks for bounding boxes on each ref so the
        model can line the snapshot up with the image.
        """
        from tabvis.utils.browser_config import is_browser_auto_visual

        page = self.active_page
        # Bounding boxes are only worth their length when a screenshot is attached to line up against.
        text = await asyncio.wait_for(
            self._build_snapshot(page, boxes=include_screenshot), timeout=self._timeout_s()
        )
        data: dict[str, Any] = {
            "snapshot": text,
            "url": page.url,
            "title": await _safe_title(page),
            "tab_count": len([p for p in self._pages() if not p.is_closed()]),
        }

        # Announce any files saved to the workspace since the last observation, and tell the agent to
        # Read them — this is how "download the file, then evaluate it" surfaces (downloads.py).
        new_downloads = self._downloads[self._downloads_reported :]
        if new_downloads:
            self._downloads_reported = len(self._downloads)
            data["downloads"] = new_downloads
            lines = "\n".join(f"  - {d['path']}" for d in new_downloads)
            data["snapshot"] = (
                f"<system-reminder>Saved {len(new_downloads)} file(s) to the download workspace. "
                f"Use the Read tool on a path to evaluate its contents:\n{lines}</system-reminder>\n"
                + text
            )

        # Decide whether the aria tree carried enough on its own.
        thin = _aria_is_thin(text) and is_browser_auto_visual()
        attach_visual = include_screenshot or thin
        if thin:
            # The snapshot said little; hand over the raw HTML so the agent can still reason about
            # structure and content the accessibility tree missed.
            html = await self._extract_html(page)
            if html:
                data["html"] = html
            data["aria_thin"] = True

        if attach_visual:
            with contextlib.suppress(Exception):
                png = await asyncio.wait_for(
                    page.screenshot(type="png"), timeout=self._timeout_s()
                )
                data["screenshot_b64"] = base64.b64encode(png).decode("ascii")
        return data

    async def _build_snapshot(self, page: Page, *, boxes: bool = False) -> str:
        self._snapshot_gen += 1
        self._snapshot_page = page
        forced = os.environ.get("TABVIS_BROWSER_SNAPSHOT_MODE")

        # 1) Public aria snapshot, AI mode — tags every node [ref=eN], resolved by the aria-ref
        #    selector engine, no DOM mutation. This is the public replacement for the private
        #    _snapshot_for_ai (kept below only as a fallback for older Playwright).
        if forced != "data":
            text = await self._aria_ai_snapshot(page, boxes=boxes)
            if text:
                self._ref_mode = "aria"
                return _truncate_snapshot(text)

        # 2) data-attr fallback — inject data-tabvis-ref and build the tree ourselves.
        self._ref_mode = "data"
        result = await self._evaluate_snapshot(page)
        return _truncate_snapshot(_render_nodes(result))

    async def _aria_ai_snapshot(self, page: Page, *, boxes: bool) -> str | None:
        """The AI aria snapshot text, or None if unavailable. Public API first, private as fallback.

        ``page.aria_snapshot(mode="ai")`` is the public API (Playwright ≳1.50); ``boxes=True`` adds a
        ``[box=x,y,w,h]`` to each node so refs can be matched to a screenshot. Older Playwright that
        lacks the kwargs (or the whole method) drops to the private ``_snapshot_for_ai``; the caller
        then drops to the data-attr path if this returns None.
        """
        aria = getattr(page, "aria_snapshot", None)
        if callable(aria):
            try:
                try:
                    text = await asyncio.wait_for(
                        aria(mode="ai", boxes=boxes), timeout=self._timeout_s()
                    )
                except TypeError:
                    # Older signature: no boxes/mode-less builds return ref-less YAML, so still ask
                    # for mode='ai' explicitly (a version without it raises again → outer except).
                    text = await asyncio.wait_for(aria(mode="ai"), timeout=self._timeout_s())
                if isinstance(text, str) and text.strip():
                    return text
            except Exception as e:  # noqa: BLE001 - fall back to the private/data-attr paths
                log_for_debugging(f"[BROWSER] public aria_snapshot failed: {e}")

        legacy = getattr(page, "_snapshot_for_ai", None)
        if callable(legacy):
            try:
                text = await asyncio.wait_for(legacy(), timeout=self._timeout_s())
                if isinstance(text, str) and text.strip():
                    return text
            except Exception as e:  # noqa: BLE001 - fall back to data-attr
                log_for_debugging(f"[BROWSER] private _snapshot_for_ai failed: {e}")
        return None

    async def capture_dom(self, *, max_bytes: int = 1_000_000) -> str:
        """The current page's DOM (HTML) for the artifacts store — best-effort, size-capped.

        Uses the same clone-and-strip pass as the snapshot helper (drops script/style/svg and defuses
        heavy data: URIs) but preserves whitespace and allows a much larger cap, so the stored DOM is
        a faithful record rather than a compact reasoning aid. Returns "" if there is no live page.
        """
        if self._context is None:
            return ""
        try:
            page = self._ensure_active_page()
            if page is None:
                return ""
            html = await asyncio.wait_for(page.evaluate(_HTML_EXTRACT_JS), timeout=self._timeout_s())
        except Exception as e:  # noqa: BLE001 - the trail must never break an action
            log_for_debugging(f"[BROWSER] capture_dom failed: {e}")
            return ""
        html = html or ""
        if len(html) > max_bytes:
            html = html[:max_bytes] + "\n<!-- [dom truncated] -->"
        return html

    async def _extract_html(self, page: Page) -> str:
        """A trimmed copy of the page HTML for the sparse-aria case — best-effort, budget-capped."""
        try:
            html = await asyncio.wait_for(
                page.evaluate(_HTML_EXTRACT_JS), timeout=self._timeout_s()
            )
        except Exception as e:  # noqa: BLE001 - a reasoning aid; never fail the observation over it
            log_for_debugging(f"[BROWSER] html extract failed: {e}")
            return ""
        html = re.sub(r"\s+", " ", html or "").strip()  # collapse whitespace to save budget
        if len(html) > _HTML_BUDGET:
            html = html[:_HTML_BUDGET] + " …[html truncated]"
        return html

    async def _evaluate_snapshot(self, page: Page) -> dict[str, Any]:
        """Run the snapshot JS, tolerating a navigation that lands mid-evaluate.

        Acting on a page routinely *causes* a navigation — pressing Enter in a search box, clicking a
        link. If that navigation commits while we are evaluating, Chromium tears the execution
        context down underneath us and Playwright raises "Execution context was destroyed". That is
        not really an error: it means the page we were about to describe has just been replaced by
        the one the agent actually wanted. So wait for the new document and describe *that* instead
        of failing the action.

        The race is timing-dependent and was always possible; humanized input makes it likely, since
        the keystrokes and the navigation no longer land in the same tick.
        """
        for attempt in (1, 2):
            try:
                return await page.evaluate(_SNAPSHOT_JS)
            except Exception as e:  # noqa: BLE001 - only the navigation race is retryable
                if attempt == 2 or not _is_navigation_race(e):
                    raise
                log_for_debugging(
                    "[BROWSER] snapshot raced a navigation; settling and re-snapshotting."
                )
                await self._settle(page)
        raise BrowserError("unreachable")  # pragma: no cover

    def resolve_ref(self, ref: str) -> Locator:
        """Resolve a ref from the latest snapshot to a Locator (mode-aware)."""
        page = self._snapshot_page
        if page is None or page.is_closed():
            raise StaleRefError(
                f"ref '{ref}' is stale (no current page); call BrowserSnapshot for fresh refs."
            )
        ref = ref.strip()
        if self._ref_mode == "aria":
            return page.locator(f"aria-ref={ref}")
        return page.locator(f"[data-tabvis-ref='{ref}']")

    async def _resolve_visible(self, ref: str) -> Locator:
        locator = self.resolve_ref(ref)
        try:
            if await locator.count() == 0:
                raise StaleRefError(
                    f"ref '{ref}' no longer matches an element (the page changed); "
                    f"call BrowserSnapshot to get fresh refs."
                )
        except StaleRefError:
            raise
        except Exception as e:  # noqa: BLE001 - resolution error => treat as stale/recoverable
            raise StaleRefError(
                f"ref '{ref}' could not be resolved ({e}); call BrowserSnapshot for fresh refs."
            ) from e
        return locator

    # ------------------------------------------------------------------ actions

    def _current_url(self) -> str | None:
        page = self._active_page
        try:
            return page.url if page is not None and not page.is_closed() else None
        except Exception:  # noqa: BLE001 — url access on a torn-down page
            return None

    async def navigate(
        self, url: str, *, action: str = "goto", wait_until: str = "load"
    ) -> dict[str, Any]:
        async with self._action_lock:
            page = self.active_page
            # Pace requests so a rapid navigation loop can't burst / DoS a host.
            nav_host = host_of(url) if action == "goto" else host_of(page.url)
            await get_request_pacer().pace(nav_host, counts_as_request=True)
            timeout = self._timeout_s()
            if action == "goto":
                response = await asyncio.wait_for(
                    page.goto(url, wait_until=wait_until), timeout=timeout
                )
                # A PDF renders in Chromium's viewer (unreadable via aria) — grab it to the workspace.
                await self._capture_pdf_navigation(response, url)
            elif action == "back":
                await asyncio.wait_for(page.go_back(wait_until=wait_until), timeout=timeout)
            elif action == "forward":
                await asyncio.wait_for(
                    page.go_forward(wait_until=wait_until), timeout=timeout
                )
            elif action == "reload":
                await asyncio.wait_for(page.reload(wait_until=wait_until), timeout=timeout)
            else:
                raise BrowserError(f"Unknown navigate action '{action}'.")
            # Let the page finish building itself before we look at it (SPAs, interstitials).
            await self._settle(page)
            return await self.observe()

    async def wait_for(
        self,
        *,
        for_text: str | None = None,
        for_gone: str | None = None,
        load_state: str | None = None,
        time_ms: int | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Wait for the page to be ready, then observe. A timeout is not an error.

        On timeout we still return the page as it stands (with ``waited_out: True``) rather than
        raising: the agent can then look at the snapshot and decide whether the page is genuinely
        stuck or just slow, which is far more useful than an exception.
        """
        async with self._action_lock:
            page = self.active_page
            # Use Playwright's OWN timeout rather than asyncio.wait_for. Cancelling from the outside
            # leaves Playwright's internal future dangling, which surfaces later as an unretrieved
            # "TargetClosedError" on stderr — noise that would corrupt --output-format stream-json.
            budget = float(timeout_ms or 15_000)
            waited_out = False
            try:
                if load_state:
                    await page.wait_for_load_state(load_state, timeout=budget)
                if for_text:
                    await page.wait_for_function(
                        "t => document.body && document.body.innerText.includes(t)",
                        arg=for_text,
                        timeout=budget,
                    )
                if for_gone:
                    await page.wait_for_function(
                        "t => !document.body || !document.body.innerText.includes(t)",
                        arg=for_gone,
                        timeout=budget,
                    )
                if time_ms:
                    await asyncio.sleep(min(time_ms, 60_000) / 1000)
            except Exception as e:  # noqa: BLE001 - a wait that fails is not fatal; show the page
                log_for_debugging(f"[BROWSER] wait_for timed out/failed: {e}")
                waited_out = True

            data = await self.observe()
            if waited_out:
                data["snapshot"] = (
                    "[the wait timed out — this is the page as it currently stands]\n"
                    + (data.get("snapshot") or "")
                )
            data["waited_out"] = waited_out
            return data

    async def _settle(self, page: Page) -> None:
        """Give a page that is still building itself a moment to finish.

        ``load`` fires long before a modern page is ready: SPAs render after it, and interstitials
        ("Just a moment…", "Loading…") replace themselves seconds later. Snapshotting immediately
        captures the wrong page and the agent reasons about a spinner. A short, bounded settle costs
        very little and makes the snapshot reflect what a human would actually see.
        """
        with contextlib.suppress(Exception):
            # Playwright's own timeout — see wait_for() for why not asyncio.wait_for.
            await page.wait_for_load_state("networkidle", timeout=_SETTLE_SECONDS * 1000)

    async def _viewport_size(self, page: Page) -> tuple[float, float]:
        """Return the live CSS viewport, including for contexts without ``viewport_size``."""
        size = getattr(page, "viewport_size", None)
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            return float(size["width"]), float(size["height"])
        try:
            value = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
            return float(value["width"]), float(value["height"])
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Could not determine the browser viewport: {e}") from e

    async def _visible_center(self, locator: Locator, page: Page) -> tuple[float, float] | None:
        """Scroll a ref into view and return the centre of its largest visible rectangle."""
        with contextlib.suppress(Exception):
            await locator.scroll_into_view_if_needed()
        await asyncio.sleep(_SCROLL_INTO_VIEW_DELAY)
        try:
            box = await locator.bounding_box()
        except Exception:  # noqa: BLE001 - a missing box triggers the JavaScript fallback
            return None
        if not box:
            return None
        width, height = await self._viewport_size(page)
        return _visible_box_center(box, width, height)

    async def _point_hits_element(
        self, locator: Locator, x: float, y: float
    ) -> bool:
        """Whether ``elementFromPoint`` hits the target or a semantically related element."""
        script = r"""
        (target, point) => {
          const hit = document.elementFromPoint(point.x, point.y);
          if (!hit) return false;
          if (hit === target || target.contains(hit) || hit.contains(target)) return true;
          const targetLabel = target.closest('label');
          const hitLabel = hit.closest && hit.closest('label');
          if (targetLabel && hitLabel === targetLabel) return true;
          const targetId = target.id;
          if (targetId && hitLabel && hitLabel.htmlFor === targetId) return true;
          const hitId = hit.id;
          const targetFor = targetLabel && targetLabel.htmlFor;
          return Boolean(hitId && targetFor && hitId === targetFor);
        }
        """
        try:
            return bool(await locator.evaluate(script, {"x": x, "y": y}))
        except Exception:  # noqa: BLE001 - inability to prove it is clear => use safe fallback
            return False

    async def _dispatch_cdp(self, page: Page, events: list[tuple[str, dict[str, Any]]]) -> bool:
        """Send raw CDP events when the active engine supports them.

        Firefox/WebKit and some remote providers do not expose CDP sessions.  Returning ``False``
        lets callers use Playwright's native input facade and, finally, JavaScript as a fallback.
        """
        session: Any = None
        try:
            session = await page.context.new_cdp_session(page)
            for method, params in events:
                await session.send(method, params)
            return True
        except Exception as e:  # noqa: BLE001 - expected on non-Chromium engines
            log_for_debugging(f"[BROWSER] CDP input unavailable ({e}); using fallback.")
            return False
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.detach()

    async def _native_click(
        self, page: Page, x: float, y: float, *, double: bool
    ) -> str:
        """Dispatch an ordered native click and return the mechanism used."""
        session: Any = None
        try:
            session = await page.context.new_cdp_session(page)
            await session.send(
                "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y}
            )
            await asyncio.sleep(_MOUSE_MOVE_DELAY)
            for count in range(1, (2 if double else 1) + 1):
                await session.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mousePressed",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": count,
                    },
                )
                await asyncio.sleep(_MOUSE_HOLD_DELAY)
                await session.send(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": x,
                        "y": y,
                        "button": "left",
                        "clickCount": count,
                    },
                )
            return "cdp"
        except Exception as e:  # noqa: BLE001 - expected on non-Chromium engines
            log_for_debugging(f"[BROWSER] CDP click unavailable ({e}); using fallback.")
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.detach()

        try:
            await page.mouse.move(x, y)
            await asyncio.sleep(_MOUSE_MOVE_DELAY)
            if double:
                await page.mouse.dblclick(x, y, delay=int(_MOUSE_HOLD_DELAY * 1000))
            else:
                await page.mouse.down(button="left")
                await asyncio.sleep(_MOUSE_HOLD_DELAY)
                await page.mouse.up(button="left")
            return "playwright-native"
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Native click failed: {e}") from e

    @staticmethod
    async def _checked_state(locator: Locator) -> bool | None:
        try:
            return await locator.evaluate(
                "el => (el.matches('input[type=checkbox],input[type=radio]') ? !!el.checked : null)"
            )
        except Exception:  # noqa: BLE001 - verification is best-effort for non-check controls
            return None

    @staticmethod
    async def _javascript_click(locator: Locator) -> None:
        try:
            await locator.evaluate("el => el.click()")
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"JavaScript click fallback failed: {e}") from e

    async def _observe_action_change(
        self, page: Page, before_url: str, before_pages: set[int]
    ) -> tuple[dict[str, Any], bool, bool]:
        """Switch to a newly opened tab, settle navigation, and return a fresh observation."""
        await asyncio.sleep(0.1)
        open_pages = [p for p in self._pages() if not p.is_closed()]
        new_pages = [p for p in open_pages if id(p) not in before_pages]
        new_tab = bool(new_pages)
        if new_pages:
            self._active_page = new_pages[-1]
            page = self._active_page
        page_changed = new_tab or page.url != before_url
        if page_changed:
            await self._settle(page)
        return await self.observe(), page_changed, new_tab

    async def click(
        self,
        ref: str | None = None,
        *,
        double: bool = False,
        coordinate_x: float | None = None,
        coordinate_y: float | None = None,
    ) -> dict[str, Any]:
        """Click a ref or viewport coordinate using native browser input.

        Ref clicks scroll into view, reject an occluded centre, and fall back to ``element.click``
        when a usable native coordinate is unavailable.  Coordinate clicks exist for canvas and
        other visual-only pages and intentionally cannot use the element fallback.
        """
        async with self._action_lock:
            has_coordinates = coordinate_x is not None or coordinate_y is not None
            if bool(ref) == has_coordinates:
                raise BrowserError("Provide either ref or both coordinate_x and coordinate_y.")
            if has_coordinates and (coordinate_x is None or coordinate_y is None):
                raise BrowserError("coordinate_x and coordinate_y must be provided together.")

            page = self.active_page
            locator = await self._resolve_visible(ref) if ref else None
            # A click frequently triggers a request (link/submit/XHR); pace it per host too.
            await get_request_pacer().pace(host_of(self._current_url()), counts_as_request=True)
            before_url = page.url
            before_pages = {id(p) for p in self._pages() if not p.is_closed()}
            checked_before = await self._checked_state(locator) if locator else None

            if locator is not None:
                point = await self._visible_center(locator, page)
                unobscured = bool(
                    point and await self._point_hits_element(locator, point[0], point[1])
                )
                if point and unobscured:
                    executed_via = await asyncio.wait_for(
                        self._native_click(page, point[0], point[1], double=double),
                        timeout=self._action_timeout_s(),
                    )
                else:
                    await asyncio.wait_for(
                        self._javascript_click(locator), timeout=self._action_timeout_s()
                    )
                    executed_via = "javascript"
            else:
                width, height = await self._viewport_size(page)
                assert coordinate_x is not None and coordinate_y is not None
                if not (0 <= coordinate_x < width and 0 <= coordinate_y < height):
                    raise BrowserError(
                        f"Click coordinate ({coordinate_x}, {coordinate_y}) is outside the "
                        f"{int(width)}x{int(height)} viewport."
                    )
                point = (float(coordinate_x), float(coordinate_y))
                executed_via = await asyncio.wait_for(
                    self._native_click(page, point[0], point[1], double=double),
                    timeout=self._action_timeout_s(),
                )

            # A checkbox/radio native click that did not toggle is not a successful interaction.
            # Retry it with DOM click, exactly once, as the documented compatibility fallback.
            checked_after = await self._checked_state(locator) if locator else None
            if locator is not None and checked_before is not None and checked_after == checked_before:
                await self._javascript_click(locator)
                checked_after = await self._checked_state(locator)
                executed_via += "+javascript-retry"

            data, page_changed, new_tab = await self._observe_action_change(
                page, before_url, before_pages
            )
            data["action_result"] = {
                "action": "double_click" if double else "click",
                "executed_via": executed_via,
                "coordinates": {"x": round(point[0], 2), "y": round(point[1], 2)},
                "page_changed": page_changed,
                "new_tab": new_tab,
                "checked_changed": (
                    checked_after != checked_before if checked_before is not None else None
                ),
                "verified": checked_after != checked_before if checked_before is not None else True,
            }
            return data

    @staticmethod
    async def _read_input_value(locator: Locator) -> str | None:
        try:
            return await locator.evaluate(
                "el => ('value' in el ? String(el.value) : (el.isContentEditable ? el.textContent : null))"
            )
        except Exception:  # noqa: BLE001
            return None

    async def _clear_input(self, locator: Locator, page: Page) -> None:
        """Clear standard, controlled, and contenteditable inputs using layered strategies."""
        clear_js = r"""
        el => {
          if ('value' in el) {
            const proto = Object.getPrototypeOf(el);
            const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
            if (descriptor && descriptor.set) descriptor.set.call(el, '');
            else el.value = '';
          } else if (el.isContentEditable) {
            el.textContent = '';
          }
          el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
        }
        """
        with contextlib.suppress(Exception):
            await locator.evaluate(clear_js)
        if (await self._read_input_value(locator)) in (None, ""):
            return

        # Rich editors and Web Components may ignore the value setter.  Select visually first.
        with contextlib.suppress(Exception):
            await locator.click(click_count=3)
            await page.keyboard.press("Backspace")
        if (await self._read_input_value(locator)) in (None, ""):
            return

        modifier = "Meta" if sys.platform == "darwin" else "Control"
        await page.keyboard.press(f"{modifier}+A")
        await page.keyboard.press("Backspace")

    @staticmethod
    async def _type_native(page: Page, text: str) -> None:
        """Type characters natively, converting line breaks to full Enter key sequences."""
        parts = re.split(r"(\r\n|\r|\n)", text)
        for part in parts:
            if not part:
                continue
            if part in ("\r\n", "\r", "\n"):
                await page.keyboard.press("Enter")
            else:
                # Playwright keyboard.type emits keydown/keypress/input/keyup for every character.
                await page.keyboard.type(part, delay=1)

    @staticmethod
    async def _dispatch_framework_input_events(locator: Locator) -> None:
        with contextlib.suppress(Exception):
            await locator.evaluate(
                """el => {
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                }"""
            )

    async def type_text(
        self, ref: str, text: str, *, clear: bool = True, submit: bool = False
    ) -> dict[str, Any]:
        async with self._action_lock:
            locator = await self._resolve_visible(ref)
            # Typing itself is no request; submitting (Enter) navigates, so pace that per host.
            await get_request_pacer().pace(host_of(self._current_url()), counts_as_request=submit)
            # Scales with len(text) — a humanized keystroke-by-keystroke fill takes far longer than
            # a plain one, and a flat cap would cancel it mid-word (see _action_timeout_s).
            timeout = self._action_timeout_s(len(text))
            page = self.active_page
            before_url = page.url
            before_pages = {id(p) for p in self._pages() if not p.is_closed()}
            previous_value = await self._read_input_value(locator)

            with contextlib.suppress(Exception):
                await locator.scroll_into_view_if_needed()
            await asyncio.sleep(_SCROLL_INTO_VIEW_DELAY)
            try:
                await asyncio.wait_for(locator.focus(), timeout=timeout)
            except Exception:  # noqa: BLE001 - a centre click is the focus fallback
                point = await self._visible_center(locator, page)
                if not point:
                    raise BrowserError(f"Could not focus ref '{ref}'.")
                await self._native_click(page, point[0], point[1], double=False)

            if clear:
                await asyncio.wait_for(self._clear_input(locator, page), timeout=timeout)
            await asyncio.wait_for(self._type_native(page, text), timeout=timeout)
            await self._dispatch_framework_input_events(locator)

            actual_value = await self._read_input_value(locator)
            expected_value = text if clear or previous_value is None else previous_value + text
            verified = actual_value is None or actual_value == expected_value
            # Contenteditable normalises CR/LF and may append a final newline; compare a normalised
            # form before treating a successful native input as a failure.
            if not verified and actual_value is not None:
                normal_actual = actual_value.replace("\r\n", "\n").rstrip("\n")
                normal_expected = expected_value.replace("\r\n", "\n").rstrip("\n")
                verified = normal_actual == normal_expected
            if not verified:
                raise BrowserError(
                    f"Typing into ref '{ref}' did not produce the requested value "
                    f"(expected {len(expected_value)} characters, got {len(actual_value or '')})."
                )
            if submit:
                await asyncio.wait_for(
                    page.keyboard.press("Enter"), timeout=self._action_timeout_s()
                )
                # Enter usually submits a form, i.e. navigates. Settle before snapshotting so the
                # agent sees the page it just asked for rather than the one it typed into.
                # wait_for_load_state("load") alone is NOT enough here: if the navigation has not
                # begun yet, the *current* page is already loaded and it returns immediately — which
                # is exactly how the snapshot ends up racing the new document (_evaluate_snapshot
                # catches what slips through).
                await self._settle(self.active_page)
            data, page_changed, new_tab = await self._observe_action_change(
                page, before_url, before_pages
            )
            data["action_result"] = {
                "action": "type",
                "executed_via": "native-keyboard",
                "characters": len(text),
                "clear": clear,
                "submit": submit,
                "value_length": len(actual_value) if actual_value is not None else None,
                "page_changed": page_changed,
                "new_tab": new_tab,
                "verified": verified,
            }
            return data

    async def send_keys(self, keys: str, *, ref: str | None = None) -> dict[str, Any]:
        """Send a special key or shortcut, optionally after focusing a fresh ref."""
        sequence = _normalize_key_sequence(keys)
        async with self._action_lock:
            page = self.active_page
            before_url = page.url
            before_pages = {id(p) for p in self._pages() if not p.is_closed()}
            if ref:
                locator = await self._resolve_visible(ref)
                with contextlib.suppress(Exception):
                    await locator.scroll_into_view_if_needed()
                try:
                    await locator.focus()
                except Exception as e:  # noqa: BLE001
                    raise BrowserError(f"Could not focus ref '{ref}' before sending keys: {e}") from e
            await get_request_pacer().pace(
                host_of(self._current_url()),
                counts_as_request=sequence.endswith("Enter"),
            )
            try:
                await asyncio.wait_for(
                    page.keyboard.press(sequence), timeout=self._action_timeout_s()
                )
            except Exception as e:  # noqa: BLE001
                raise BrowserError(f"Could not send keys '{keys}': {e}") from e
            data, page_changed, new_tab = await self._observe_action_change(
                page, before_url, before_pages
            )
            data["action_result"] = {
                "action": "keys",
                "keys": sequence,
                "page_changed": page_changed,
                "new_tab": new_tab,
                "verified": True,
            }
            return data

    async def _scroll_position(self, page: Page, locator: Locator | None) -> float | None:
        try:
            if locator is not None:
                return float(await locator.evaluate("el => el.scrollTop"))
            return float(await page.evaluate("() => window.scrollY"))
        except Exception:  # noqa: BLE001
            return None

    async def _scroll_once(
        self,
        page: Page,
        pixels: float,
        *,
        point: tuple[float, float],
        locator: Locator | None,
    ) -> str:
        """Perform one native scroll step, with the documented JavaScript fallback."""
        x, y = point
        if locator is None:
            native = await self._dispatch_cdp(
                page,
                [
                    (
                        "Input.synthesizeScrollGesture",
                        {
                            "x": x,
                            "y": y,
                            "xDistance": 0,
                            "yDistance": -pixels,
                            "speed": 50000,
                        },
                    )
                ],
            )
        else:
            native = await self._dispatch_cdp(
                page,
                [
                    (
                        "Input.dispatchMouseEvent",
                        {
                            "type": "mouseWheel",
                            "x": x,
                            "y": y,
                            "deltaX": 0,
                            "deltaY": pixels,
                        },
                    )
                ],
            )
        if native:
            return "cdp"

        try:
            await page.mouse.move(x, y)
            await page.mouse.wheel(0, pixels)
            return "playwright-native"
        except Exception as e:  # noqa: BLE001 - JavaScript is the final compatibility fallback
            log_for_debugging(f"[BROWSER] native scroll failed ({e}); using JavaScript.")

        try:
            if locator is not None:
                await locator.evaluate("(el, dy) => el.scrollBy({top: dy, behavior: 'instant'})", pixels)
            else:
                await page.evaluate("dy => window.scrollBy({top: dy, behavior: 'instant'})", pixels)
            return "javascript"
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Scroll failed: {e}") from e

    async def scroll(
        self, *, down: bool = True, pages: float = 1.0, ref: str | None = None
    ) -> dict[str, Any]:
        """Scroll the page or a ref's container in page-sized native input steps."""
        if not (0 < pages <= 100):
            raise BrowserError("pages must be greater than 0 and no more than 100.")
        async with self._action_lock:
            page = self.active_page
            locator = await self._resolve_visible(ref) if ref else None
            width, height = await self._viewport_size(page)
            if locator is not None:
                point = await self._visible_center(locator, page)
                if point is None:
                    raise BrowserError(f"Could not find a visible scroll point for ref '{ref}'.")
            else:
                point = (width / 2, height / 2)

            before = await self._scroll_position(page, locator)
            direction = 1 if down else -1
            full_steps = int(pages)
            fractions = [1.0] * full_steps
            remainder = pages - full_steps
            if remainder > 1e-9:
                fractions.append(remainder)
            mechanisms: list[str] = []
            for index, fraction in enumerate(fractions):
                mechanisms.append(
                    await self._scroll_once(
                        page,
                        direction * height * fraction,
                        point=point,
                        locator=locator,
                    )
                )
                if index < len(fractions) - 1:
                    await asyncio.sleep(_SCROLL_STEP_DELAY)

            after = await self._scroll_position(page, locator)
            data = await self.observe()
            data["action_result"] = {
                "action": "scroll",
                "direction": "down" if down else "up",
                "pages": pages,
                "target": ref or "page",
                "executed_via": "+".join(dict.fromkeys(mechanisms)),
                "position_changed": (
                    after != before if before is not None and after is not None else None
                ),
                # No movement at the document boundary is still a verified delivered gesture.
                "verified": True,
            }
            return data

    async def snapshot(self, *, include_screenshot: bool = False) -> dict[str, Any]:
        async with self._action_lock:
            return await self.observe(include_screenshot=include_screenshot)


# --------------------------------------------------------------------------- helpers


def _visible_box_center(
    box: dict[str, float], viewport_width: float, viewport_height: float
) -> tuple[float, float] | None:
    """Centre of the box/viewport intersection, clamped to valid CSS coordinates."""
    left = max(0.0, float(box.get("x", 0.0)))
    top = max(0.0, float(box.get("y", 0.0)))
    right = min(viewport_width, float(box.get("x", 0.0)) + float(box.get("width", 0.0)))
    bottom = min(viewport_height, float(box.get("y", 0.0)) + float(box.get("height", 0.0)))
    if right <= left or bottom <= top or viewport_width <= 0 or viewport_height <= 0:
        return None
    x = max(0.0, min(viewport_width - 1, (left + right) / 2))
    y = max(0.0, min(viewport_height - 1, (top + bottom) / 2))
    return x, y


_KEY_ALIASES = {
    "ctrl": "Control",
    "control": "Control",
    "cmd": "Meta",
    "command": "Meta",
    "meta": "Meta",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": "Space",
    "home": "Home",
    "end": "End",
}
_KEY_MODIFIERS = frozenset({"Control", "Meta", "Alt", "Shift"})
_KEY_NAMES = frozenset(set(_KEY_ALIASES.values()) - set(_KEY_MODIFIERS))


def _normalize_key_sequence(keys: str) -> str:
    """Validate and canonicalise a Playwright/CDP key chord such as ``Control+A``."""
    raw = (keys or "").strip()
    if not raw:
        raise BrowserError("keys must not be empty.")
    parts = [part.strip() for part in raw.split("+")]
    if any(not part for part in parts):
        raise BrowserError(f"Invalid key sequence '{keys}'.")

    normalized: list[str] = []
    main_keys = 0
    for part in parts:
        canonical = _KEY_ALIASES.get(part.lower())
        if canonical is None and len(part) == 1:
            canonical = part.upper() if part.isalpha() else part
        if canonical is None or (canonical not in _KEY_MODIFIERS and canonical not in _KEY_NAMES and len(canonical) != 1):
            raise BrowserError(f"Unsupported key '{part}'.")
        if canonical not in _KEY_MODIFIERS:
            main_keys += 1
        if canonical in normalized:
            raise BrowserError(f"Duplicate key '{part}' in '{keys}'.")
        normalized.append(canonical)
    if main_keys != 1:
        raise BrowserError("A key sequence must contain exactly one non-modifier key.")
    return "+".join(normalized)


def _display_available() -> bool:
    """Best-effort: is there a display a headed browser could open a window on?

    macOS and Windows always have one. On Linux/BSD it needs an X or Wayland server, advertised via
    ``$DISPLAY`` / ``$WAYLAND_DISPLAY``. Deliberately conservative — it only returns False when we
    are *sure* there is no display, so the exception-based fallback still covers a display that is
    advertised but broken (a stale ``$DISPLAY``, a dead X server).
    """
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _playwright_driver_pid(pw: Any, context: Any = None) -> int | None:
    """Best-effort: the Playwright node driver's pid (private API — None if unavailable).

    Falls back to digging it out of the *context*, because under the cloak engine cloakbrowser owns
    the Playwright instance and we never hold one — but the context rides the same connection.
    """
    for obj in (pw, context):
        if obj is None:
            continue
        try:
            return int(obj._impl_obj._connection._transport._proc.pid)
        except Exception:  # noqa: BLE001 - private API; absence is fine
            continue
    return None


def _is_navigation_race(error: Exception) -> bool:
    """Whether a Playwright error is just 'the page navigated while we were looking at it'.

    Matched on the message because Playwright raises a generic ``Error`` for both, with no dedicated
    exception class to catch.
    """
    msg = str(error).lower()
    return "execution context was destroyed" in msg or (
        "destroyed" in msg and "navigation" in msg
    )


async def _safe_title(page: Page) -> str:
    with contextlib.suppress(Exception):
        return await page.title()
    return ""


def _aria_is_thin(snapshot: str) -> bool:
    """Whether the accessibility snapshot conveys too little to reason from on its own.

    True for a page whose aria tree is near-empty — a canvas game, a maps/whiteboard app, an
    image-only page, or a render that produced no accessible structure. Measured by two cheap
    signals on the snapshot text: how many ref-tagged nodes it has, and how long it is. When both
    are below their thresholds the tree is 'not enough', and observe() adds a screenshot + HTML.
    """
    s = (snapshot or "").strip()
    if not s:
        return True
    return s.count("[ref=") <= _ARIA_THIN_MAX_REFS and len(s) < _ARIA_THIN_MAX_CHARS


def _truncate_snapshot(text: str) -> str:
    if len(text) <= _SNAPSHOT_CHAR_BUDGET:
        return text
    head = text[:_SNAPSHOT_CHAR_BUDGET]
    return (
        head
        + "\n… [snapshot truncated to stay within the tool-result size limit; "
        "act on a visible element or navigate to narrow the page]"
    )


def _render_nodes(result: dict[str, Any]) -> str:
    """Render the page as READABLE TEXT plus the interactive elements you can act on.

    Both halves matter. The text is what the agent came to read; the ref-tagged elements are what
    it can click and type into. A snapshot with only elements is a browser agent that cannot read.
    """
    parts: list[str] = []

    text = (result.get("text") or "").strip()
    if text:
        if len(text) > _TEXT_BUDGET:
            text = text[:_TEXT_BUDGET] + " …[page text truncated]"
        parts.append("--- page text ---\n" + text)

    lines: list[str] = []
    for node in result.get("nodes") or []:
        role = node.get("role") or "generic"
        name = node.get("name") or ""
        line = f'- {role} "{name}" [ref={node.get("ref")}]'
        value = node.get("value")
        if value:
            line += f' (value: "{value}")'
        lines.append(line)
    parts.append(
        "--- interactive elements (act on these by ref) ---\n" + "\n".join(lines)
        if lines
        else "--- interactive elements ---\n(none found)"
    )

    if not text and not lines:
        return "(the page appears to be empty — it may still be loading; try BrowserWait)"
    return "\n\n".join(parts)
